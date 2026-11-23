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
import signal
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import FrameType
from typing import Any, NoReturn

from . import __version__, watches
from .config import ConfigError, LoadedConfig, load_config
from .log import NULL_LOG, Log
from .refs import (
    PrId,
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
from .state_dir import StateDir, StateDirError, open_existing_state_dir, open_state_dir
from .store import Store, StoreError, open_existing_store, open_store
from .text import MAX_MESSAGE_CHARS, one_line, scrub_text

PROG = "pr-watch"
DESCRIPTION = "Watches PRs for changes and exposes them as events and hooks."

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

    def __init__(
        self,
        message: str,
        usage: str = "",
        kind: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        merged = {**({"kind": kind} if kind else {}), **(details or {})}
        super().__init__("usage", message, details=merged or None)
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
    loaded: LoadedConfig
    # Both are opened by `_execute`. Read-only commands (list/show/events/export) get `state` and
    # `store` None when the state dir or state.db is missing (and in unit tests); those handlers
    # must treat a None store as an empty store (owned by #19, #27).
    state: StateDir | None = None
    store: Store | None = None
    log: Log = NULL_LOG  # `<state dir>/log.jsonl`; logging never fails a command

    def emit(self, data: Any, human: str | None = None) -> None:
        """Print a command result: a JSON document with `--json`, else `human` unless `--quiet`."""
        wanted = self.json or (human is not None and not self.quiet)
        if wanted and sys.stdout is None:  # fd 1 closed at start: `print` would drop it silently
            raise OutputError
        try:
            if self.json:
                print(json.dumps(data), flush=True)
            elif human is not None and not self.quiet:
                print(scrub_text(human), flush=True)
        except OSError as exc:
            raise OutputError from exc


Handler = Callable[[Context, argparse.Namespace], int]


# Commands that only read state: they open (and so verify) the state directory but write no
# log record, so a consumer that polls `events` or `list` cannot fill log.jsonl with the same
# configuration warnings on every call.
READ_ONLY_COMMANDS = frozenset({"list", "show", "events", "export", "browse", "validate"})

# One entry per command. A later issue replaces its stub with the real handler.
HANDLERS: dict[str, Handler] = {
    "watch add": lambda ctx, args: _watch_add(ctx, args),
    "watch remove": lambda ctx, args: _watch_remove(ctx, args),
    "label add": lambda ctx, args: _label_add(ctx, args),
    "label remove": lambda ctx, args: _label_remove(ctx, args),
    "list": lambda ctx, args: _list(ctx, args),
    "prune": lambda ctx, args: _prune(ctx, args),
    "poll": lambda ctx, args: _poll(ctx, args),
    "events": lambda ctx, args: _events(ctx, args),
    "show": lambda ctx, args: _show(ctx, args),
    "export": lambda ctx, args: _export(ctx, args),
    "browse": lambda ctx, args: _browse(ctx, args),
    "validate": lambda ctx, args: _validate(ctx, args),
    "cron install": lambda ctx, args: _cron_install(ctx, args),
    "cron uninstall": lambda ctx, args: _cron_uninstall(ctx, args),
}


# --- watch remove, label add, label remove -----------------------------------------------------

# Commands whose `--dry-run` is read-only.
DRY_RUN_COMMANDS = frozenset({"prune", "cron install", "cron uninstall"})
# Commands that never read or write state.db: they must not fail on a bad or newer database.
NO_STORE_COMMANDS = frozenset({"cron install", "cron uninstall", "browse", "validate"})


def store_error_to_cli(exc: StoreError) -> CliError:
    """`StoreTooNewError` is `store_too_new`; every other store failure is `internal` (the message
    is the store's own one-line text, never database content)."""
    return CliError(exc.code, str(exc))


def _store(ctx: Context) -> Store:
    if ctx.store is None:
        raise CliError("internal", "the state store is not open")
    return ctx.store


def _watch_add(ctx: Context, args: argparse.Namespace) -> int:
    from .watch_add import watch_add  # lazy: it imports this module (failures needs CliError)

    return watch_add(ctx, args)


def _prune(ctx: Context, args: argparse.Namespace) -> int:
    from .prune import prune_command  # lazy: it imports this module

    return prune_command(ctx, args)


def _cron_install(ctx: Context, args: argparse.Namespace) -> int:
    from .cron import install_command  # lazy: it imports this module

    return install_command(ctx, args)


def _cron_uninstall(ctx: Context, args: argparse.Namespace) -> int:
    from .cron import uninstall_command  # lazy: it imports this module

    return uninstall_command(ctx, args)


def _poll(ctx: Context, args: argparse.Namespace) -> int:
    from .poller import poll_command  # lazy: it imports this module

    return poll_command(ctx, args)


def _browse(ctx: Context, args: argparse.Namespace) -> int:
    from .browse import browse_command  # lazy: it imports this module

    return browse_command(ctx, args)


def _validate(ctx: Context, args: argparse.Namespace) -> int:
    from .validate import validate_command  # lazy: it imports this module

    return validate_command(ctx, args)


def _list(ctx: Context, args: argparse.Namespace) -> int:
    from .listing import list_command  # lazy: it imports this module

    return list_command(ctx, args)


def _events(ctx: Context, args: argparse.Namespace) -> int:
    from .events import events_command  # lazy: it imports this module

    return events_command(ctx, args)


def _export(ctx: Context, args: argparse.Namespace) -> int:
    from .events import export_command  # lazy: it imports this module

    return export_command(ctx, args)


def _show(ctx: Context, args: argparse.Namespace) -> int:
    from .show import show_command  # lazy: it imports this module

    return show_command(ctx, args)


def _remove_target(conn: Any, args: argparse.Namespace) -> tuple[str | None, str, str | None]:
    """`(watch id, reference, reason when there is no watch)` of a `watch remove` command."""
    if args.pr is None:
        key = watch_key(args.host or [], args.repo or [])
        return watches.find_watch(conn, watches.QUERY_KIND, key), key, "not_held"
    found = watches.resolve_stored_pr(conn, args.pr)
    if found is None:
        ref = args.pr.value if isinstance(args.pr, PrId) else args.pr.canonical
        return None, ref, "pr_unknown"
    pr_id, pr = found
    return watches.pr_watch_id(conn, pr_id), pr, "not_held"


def _watch_remove(ctx: Context, args: argparse.Namespace) -> int:
    store = _store(ctx)
    # Resolve under a read view first: a command with nothing to change never takes the write
    # lock. When there is something to remove, resolve again inside the transaction (authoritative).
    watch_id, reference, reason = _remove_target(store.conn, args)
    removal = watches.Removal(held=False, watch_ended=False, prs_dropped=0)
    view = None
    if watch_id is not None and watches.holds(store.conn, watch_id, args.owner):
        with store.transaction() as conn:
            watch_id, reference, reason = _remove_target(conn, args)
            if watch_id is not None:
                removal = watches.remove_owner(conn, watch_id, args.owner)
            if removal.held and not removal.watch_ended and watch_id is not None:
                view = watches.watch_view(conn, watch_id)
    result = {
        "watch": view,
        "owner": args.owner,
        "changed": removal.held,
        "reason": None if removal.held else reason,
        "watch_ended": removal.watch_ended,
        "prs_dropped": removal.prs_dropped,
        "reference": reference,
    }
    # The warning is not an informational line: --quiet silences the result only (Global flags).
    if not removal.held and reason == "pr_unknown":
        _stderr_line(f"{PROG}: warning: no stored PR matches {reference}\n")
    if removal.watch_ended:
        human = f"removed {args.owner} from {reference}; the watch ended"
    elif removal.held:
        human = f"removed {args.owner} from {reference}"
    else:
        human = f"{args.owner} held no reference to {reference}"
    ctx.emit(result, human=human)
    return 0


def _label_watch(conn: Any, args: argparse.Namespace) -> str | None:
    """The watch a label command names (by id, or by a per-PR watch's reference), or None."""
    if args.watch is not None:
        return args.watch if watches.watch_exists(conn, args.watch) else None
    found = watches.resolve_stored_pr(conn, args.pr)
    return None if found is None else watches.pr_watch_id(conn, found[0])


def _label_add_checked(conn: Any, args: argparse.Namespace) -> str:
    """The watch to label, after the checks that refuse (no watch, owner not held)."""
    watch_id = _label_watch(conn, args)
    if watch_id is None:
        raise CliError("not_found", "no watch to label", details={"kind": "no_watch"})
    if not watches.holds(conn, watch_id, args.owner):
        raise CliError(
            "not_found",
            "the owner holds no reference to the watch",
            details={"kind": "owner_not_held"},
        )
    return watch_id


def _label_add(ctx: Context, args: argparse.Namespace) -> int:
    store = _store(ctx)
    _label_add_checked(store.conn, args)  # refusals need no write lock
    with store.transaction() as conn:
        watch_id = _label_add_checked(conn, args)
        count = watches.label_count_after(conn, watch_id, args.labels)
        if count > watches.MAX_LABELS_PER_WATCH:  # counted here, before anything is written
            raise UsageError(
                f"a watch carries at most {watches.MAX_LABELS_PER_WATCH} labels",
                kind="too_many_labels",
                details={"limit": watches.MAX_LABELS_PER_WATCH, "count": count},
            )
        changed = watches.add_labels(conn, watch_id, args.owner, args.labels)
        view = watches.watch_view(conn, watch_id)
    ctx.emit(
        {"watch": view, "owner": args.owner, "changed": changed},
        human=f"labels added to {watch_id}" if changed else f"{watch_id} already has the labels",
    )
    return 0


def _label_remove_reason(conn: Any, args: argparse.Namespace) -> tuple[str | None, str | None]:
    """`(watch id, reason)` of a `label remove`: the reason is set when there is nothing to do
    (`no_watch`, `not_held`); the watch id is None only for `no_watch`."""
    watch_id = _label_watch(conn, args)
    if watch_id is None:
        return None, "no_watch"
    if not watches.holds(conn, watch_id, args.owner):
        return watch_id, "not_held"
    return watch_id, None


def _label_remove(ctx: Context, args: argparse.Namespace) -> int:
    store = _store(ctx)
    changed = False
    view = None
    watch_id, reason = _label_remove_reason(store.conn, args)
    if reason is None and watch_id is not None:
        own = set(watches.labels_of(store.conn, watch_id, args.owner))
        if own & {watches.label_text(label) for label in args.labels}:
            with store.transaction() as conn:  # something to remove: re-resolve under the lock
                watch_id, reason = _label_remove_reason(conn, args)
                if reason is None and watch_id is not None:
                    changed = watches.remove_labels(conn, watch_id, args.owner, args.labels)
                    view = watches.watch_view(conn, watch_id)
        else:
            view = watches.watch_view(store.conn, watch_id)
    if reason == "no_watch":
        human = "no watch matches the PR"
    elif reason == "not_held":
        human = f"{args.owner} holds no reference to {watch_id}"
    elif changed:
        human = f"removed the labels from {watch_id}"
    else:
        human = f"{args.owner} has none of the labels on {watch_id}"
    ctx.emit(
        {"watch": view, "owner": args.owner, "changed": changed, "reason": reason}, human=human
    )
    return 0


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
        help="no human result output on success (warnings still print)",
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

    p = _command(sub, "browse", "browse", "browse watches, PRs and events in a terminal UI")
    p.add_argument("--focus", metavar="PR", help="open on this stored PR")

    p = _command(sub, "validate", "validate", "check the config and the environment (offline)")
    p.add_argument(
        "--probe", action="store_true", help="also ask forgectl whoami (may reach hosts)"
    )

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
    p.add_argument("--dry-run", action="store_true", help="report only")
    p = _command(cron, "uninstall", "cron uninstall", "remove the tagged crontab block")
    p.add_argument("--dry-run", action="store_true", help="report only")

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


def _check_poll_target(args: argparse.Namespace) -> None:
    """`--pr` and `--repo` name two different scopes: never both."""
    if args.pr is not None and args.repo is not None:
        raise _shape_error("bad_combination", "--pr and --repo cannot be combined")


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
    "poll": _check_poll_target,
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


def _check_browse_values(args: argparse.Namespace) -> None:
    args.focus = _one(args.focus, parse_reference)


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
    "browse": _check_browse_values,
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
    except (OSError, ValueError, AttributeError):  # closed (fd 2 closed: `sys.stderr` is None)
        _silence_stderr()  # nowhere left to report it


def _silence_stderr() -> None:
    """A failed stderr write leaves the text in the stream's buffer (on Python 3.12 fd 2 can be
    some other, unwritable descriptor, not None), so the interpreter's final flush fails again and
    the exit code becomes 120. Swap in a sink so the shutdown flush is quiet."""
    try:
        sys.stderr = open(os.devnull, "w")  # noqa: SIM115 - kept open for the shutdown flush
    except OSError:
        pass


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
    # `--dry-run` of `prune` and `cron install|uninstall` is the read-only form of a mutating
    # command: it creates nothing (`prune` opens `mode=ro`; the cron commands take no lock).
    read_only = args.command in READ_ONLY_COMMANDS or (
        args.command in DRY_RUN_COMMANDS and args.dry_run
    )
    try:
        # Read-only commands create nothing: a missing state directory or state.db is an empty
        # store (`ctx.state` / `ctx.store` None); an existing one is checked as usual.
        # `browse` opens nothing here: its terminal and extra checks come first, and the TUI shows
        # an unreadable store as a failed read (its own read-only open does the reading).
        if args.command == "browse":
            state = None
        else:
            state = open_existing_state_dir() if read_only else open_state_dir()
    except StateDirError as exc:
        raise CliError("state_unsafe", str(exc)) from None
    store: Store | None = None
    try:
        if args.command not in NO_STORE_COMMANDS and state is not None:
            try:
                store = open_existing_store(state) if read_only else open_store(state)
            except StateDirError as exc:
                raise CliError("state_unsafe", str(exc)) from None
            except StoreError as exc:
                raise store_error_to_cli(exc) from None
        log = Log(
            state, on_error=None if args.quiet else lambda line: _stderr_line(f"{PROG}: {line}\n")
        )
        if not read_only:
            log.config_warnings(loaded.warnings)
        ctx = Context(
            json=args.json,
            quiet=args.quiet,
            verbose=args.verbose,
            loaded=loaded,
            state=state,
            store=store,
            log=log,
        )
        return HANDLERS[args.command](ctx, args)
    finally:
        if store is not None:
            store.close()
        if state is not None:
            state.close()


class Signaled(BaseException):
    """A SIGTERM, SIGINT or SIGHUP reached a command that does not handle signals itself (`poll`
    does). A BaseException so no `except Exception` hides it; `main` turns it into exit 128+N."""

    def __init__(self, number: int) -> None:
        super().__init__(number)
        self.number = number


_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


_FIRST_SIGNAL: list[int] = []  # the first stop signal of this process; later ones are ignored


def _raise_signaled(number: int, frame: FrameType | None) -> None:
    """First signal wins: it raises `Signaled`; later ones neither raise again nor change the
    code, so they cannot interrupt the unwind (the child's shutdown, the rollback)."""
    if _FIRST_SIGNAL:
        return
    _FIRST_SIGNAL.append(number)
    raise Signaled(number)


def _install_signal_handlers() -> dict[int, Any]:
    """Make the three stop signals raise `Signaled` (so transactions roll back and the store
    closes); only the main thread can. Returns what to restore."""
    if threading.current_thread() is not threading.main_thread():
        return {}
    return {n: signal.signal(n, _raise_signaled) for n in _SIGNALS}


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    _FIRST_SIGNAL.clear()
    saved = _install_signal_handlers()
    try:
        return _main(args)
    except Signaled as stop:
        return 128 + stop.number  # the signal contract: quiet, no traceback
    except KeyboardInterrupt:
        return 128 + signal.SIGINT
    finally:
        for number, old in saved.items():
            signal.signal(number, old)


def _main(args: list[str]) -> int:
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
