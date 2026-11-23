"""The one failure table (docs/interface.md: Failures): what every forgectl outcome means.

`ROWS` is the table as data: one row per outcome class, and in each row one `Effect` per kind of
call. The poller (and the direct commands) never branch on a forgectl code themselves; they ask
`decide`, `decide_code`, `decide_local` or `decide_tool` for a `Decision` and act on its effect:

- `scope`: what the failure is about (`pr`, `repo`, `host`, the whole `poll`, or `none`);
- `counter`: `increment` or `reset` (only the PR call ever changes a counter) or `none`;
- `pause`: `retry_after` means the host is paused (`host_pause` computes how long);
- `cancel_host`: the rest of the host's queue and listings are cancelled this cycle;
- `failed_kind`: the `failed[]` entry of the poll report (`pr`, `repo`, `query`, `host`);
- `record`: the failure is eligible for a `snapshot_failed` record and `last_failure_code`
  (`should_record` says whether this failure is actually written); `prominent`: a prominent report
  entry and a `post-poll` mention; `log_error`: a `log.jsonl` error text; `warn_contract`: a
  `contract` warning; `fallback_show`: the over-cap `show` fallback;
- `poll_exit`: 12 when this effect makes `poll` exit 12, else 0; `stop_poll`: the poll ends now;
- `commit` / `use_result`: a success (commit it) or a partial `prs`/`whoami` object (use it);
- `cli_code`: the error envelope code a direct command (`watch add`) ends with, whose exit code is
  `cli.EXIT_CODES[cli_code]`; `to_cli_error` builds the `CliError`.

Call kinds: `pr` (`snapshot` or `show` of one PR), `repo` (`prs --repo`), `query` (`prs --host`)
and `whoami`. The interface's third column covers both `query` and `whoami`; they differ only in
the `failed[]` kind.

A failure produces no event and never changes a PR's state (the one exception, `unreachable`, is
stored by `snapshots._count` with the status change: `health.py`): no effect here commits anything
but a success, `not_found` means unknown (never closed), and whole-host failures never touch a
counter. Code the table does not know is the `transient` row (the `internal` row of the
interface).

Judgment calls where the interface is silent (kept conservative):

- Exit 137 is the `transient` row with code `killed` (the interface lists it there); any other
  exit above 128 is `interrupted`, with code `interrupted` and no counter.
- A permanent PR failure (`not_found`, `permission_denied`, `unknown_host`, `pr_id_changed`) is
  polled as usual and counted until the PR becomes `unreachable`.
- The reported code is forgectl's own code (usage, invalid_pr and config_invalid included), except
  that `not_found` on a host call is the `internal` row and is reported as `internal`.
- A `partial` result of a PR call, or at no exit code, is a `contract` failure.
- A `refused` code with a reason ends a direct command as `refused` with that reason; the poller
  still uses the default row.
- `usage` and `invalid_pr` of a host call are the `transient` host row plus a `contract` warning;
  only forgectl's own `config_invalid` stops the poll.
- A `refused` code with any reason but `unknown_host` is the default row for the poller.
- `pr_id_changed` ends a direct command as `host_failed` (exit 12, nothing recorded).
- Every counted per-PR failure is eligible for a `snapshot_failed` record, written only as
  `should_record` says (first failure of a streak, a code change, a multiple of
  `unreachable_after`); `interrupted` PRs are listed in `failed[]` (left unchecked) without one.
- `usage`, `invalid_pr` and `config_invalid` of a direct command end as `internal` (exit 1): direct
  commands validate references first, so forgectl rejecting one is a bug, not host trouble.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Final

from pr_watch.cli import CliError
from pr_watch.forgectl_client import INSTALL, ForgectlError, Outcome, ToolError, ToolTooOld

CALLS: Final = ("pr", "repo", "query", "whoami")
SCOPES: Final = ("none", "pr", "repo", "host", "poll")
MAX_PAUSE: Final = 3600.0  # a larger retry_after is clamped
BACKOFF_START: Final = 60
BACKOFF_MAX: Final = 900
_CODE: Final = re.compile(r"[a-z_]{1,64}", re.ASCII)
_WRONG_HOST_LOG: Final = "the token may have reached another host and should be rotated"


@dataclass(frozen=True)
class Effect:
    scope: str = "none"
    counter: str = "none"
    pause: str = "none"
    cancel_host: bool = False
    failed_kind: str | None = None
    record: bool = False
    prominent: bool = False
    poll_exit: int = 0
    log_error: str | None = None
    warn_contract: bool = False
    fallback_show: bool = False
    commit: bool = False
    use_result: bool = False
    stop_poll: bool = False
    cli_code: str | None = None


@dataclass(frozen=True)
class Row:
    """One outcome class. `doc` is the start of its first cell in the interface table (None for a
    row the table does not list); `code` is the failure code reported when none is given."""

    doc: str | None
    code: str | None
    effects: Mapping[str, Effect]


def _row(doc: str | None, code: str | None, effects: Mapping[str, Effect]) -> Row:
    return Row(doc, code, MappingProxyType(dict(effects)))


def _all(effect: Effect) -> dict[str, Effect]:
    return dict.fromkeys(CALLS, effect)


_FAILED = Effect(cli_code="host_failed")
_PR_COUNTED = replace(_FAILED, scope="pr", counter="increment", failed_kind="pr", record=True)
_REPO_FAILED = replace(_FAILED, scope="repo", failed_kind="repo")
_QUERY_FAILED = replace(_FAILED, scope="host", failed_kind="query", poll_exit=12)
_WHOAMI_FAILED = replace(_FAILED, scope="host", failed_kind="host", poll_exit=12)
_HOST_WHOLE = replace(_FAILED, scope="host", cancel_host=True, failed_kind="host", poll_exit=12)
_TRANSIENT = {
    "pr": _PR_COUNTED,
    "repo": _REPO_FAILED,
    "query": _QUERY_FAILED,
    "whoami": _WHOAMI_FAILED,
}
_PERMANENT_PR = _PR_COUNTED


def _with(base: Mapping[str, Effect], **changes: Any) -> dict[str, Effect]:
    return {call: replace(effect, **changes) for call, effect in base.items()}


def _pr_only(pr: Effect, others: Mapping[str, Effect]) -> dict[str, Effect]:
    return {**others, "pr": pr}


_D_SUCCESS = "success (exit 0 object)"
_D_RATE = "`rate_limited` (exit 12"
_D_HOST = "`auth_required`, forgectl `tool_missing`"
_D_REJECT = "`host_rejected`, `unsupported_host_version`"
_D_TRANSIENT = "`network_error`, `upstream_error`"
_D_NOT_FOUND = "`not_found` (exit 10)"
_D_UNKNOWN_HOST = "`unknown_host` (exit 20)"
_D_PR_ID = "`pr_id_changed` (local"
_D_SIGNALED = "exit 128+N while"
_D_USAGE = "exit 2 (`usage`"
_D_VERSION_RUN = "`--version` cannot run"
_D_VERSION_OLD = "version below the minimum"

_TOOL_WIDE = Effect(scope="poll", stop_poll=True, poll_exit=12)
_USAGE_PR = replace(_PR_COUNTED, warn_contract=True, cli_code="internal")
_USAGE_REPO = replace(_REPO_FAILED, warn_contract=True, cli_code="internal")

ROWS: Final[Mapping[str, Row]] = MappingProxyType(
    {
        "success": _row(
            _D_SUCCESS,
            None,
            {
                "pr": Effect(scope="pr", counter="reset", commit=True),
                "repo": Effect(scope="repo", commit=True),
                "query": Effect(scope="host", commit=True),
                "whoami": Effect(scope="host", commit=True),
            },
        ),
        # Exit 12 with a partial `prs` or `whoami` object: use it; its `errors` entries are
        # decided one by one with `decide_code` and the same host call kind.
        "partial": _row(
            None,
            None,
            {
                "repo": Effect(scope="repo", use_result=True),
                "query": Effect(scope="host", use_result=True),
                "whoami": Effect(scope="host", use_result=True),
            },
        ),
        "rate_limited": _row(
            _D_RATE,
            "rate_limited",
            _all(replace(_FAILED, scope="host", pause="retry_after", cancel_host=True)),
        ),
        "host_down": _row(_D_HOST, "auth_required", _all(_HOST_WHOLE)),
        "forgectl_tool_missing": _row(
            _D_HOST, "tool_missing", _all(replace(_HOST_WHOLE, cli_code="tool_missing"))
        ),
        "host_rejected": _row(
            _D_REJECT,
            "host_rejected",
            _pr_only(replace(_PR_COUNTED, prominent=True), _all(_HOST_WHOLE)),
        ),
        "wrong_host_response": _row(
            _D_REJECT,
            "wrong_host_response",
            _all(replace(_HOST_WHOLE, log_error=_WRONG_HOST_LOG)),
        ),
        "transient": _row(_D_TRANSIENT, "internal", _TRANSIENT),
        "contract": _row(_D_TRANSIENT, "contract", _with(_TRANSIENT, cli_code="contract")),
        "too_large": _row(
            _D_TRANSIENT,
            "too_large",
            _pr_only(replace(_PR_COUNTED, fallback_show=True), _TRANSIENT),
        ),
        "not_found": _row(
            _D_NOT_FOUND,
            "not_found",
            _pr_only(replace(_PERMANENT_PR, cli_code="not_found"), _TRANSIENT),
        ),
        "permission_denied": _row(
            _D_NOT_FOUND, "permission_denied", _pr_only(_PERMANENT_PR, _TRANSIENT)
        ),
        "unknown_host": _row(
            _D_UNKNOWN_HOST,
            "refused",
            _with(
                {
                    "pr": _PERMANENT_PR,
                    "repo": _REPO_FAILED,
                    "query": _QUERY_FAILED,
                    "whoami": _WHOAMI_FAILED,
                },
                cli_code="refused",
            ),
        ),
        "pr_id_changed": _row(_D_PR_ID, "pr_id_changed", {"pr": _PERMANENT_PR}),
        "interrupted": _row(
            _D_SIGNALED,
            "interrupted",
            {
                "pr": replace(_FAILED, scope="pr", failed_kind="pr"),
                "repo": replace(_FAILED, scope="repo", failed_kind="repo"),
                "query": replace(_FAILED, scope="host", failed_kind="query"),
                "whoami": replace(_FAILED, scope="host", failed_kind="host"),
            },
        ),
        "usage_rejected": _row(
            _D_USAGE,
            "usage",
            _pr_only(_USAGE_PR, {**_TRANSIENT, "repo": _USAGE_REPO})
            | {
                call: replace(_TRANSIENT[call], warn_contract=True, cli_code="internal")
                for call in ("query", "whoami")
            },
        ),
        "config_invalid": _row(
            _D_USAGE,
            "config_invalid",
            {
                "pr": _USAGE_PR,
                "repo": _USAGE_REPO,
                "query": replace(_TOOL_WIDE, cli_code="host_failed", failed_kind="host"),
                "whoami": replace(_TOOL_WIDE, cli_code="host_failed", failed_kind="host"),
            },
        ),
        "tool_missing": _row(
            _D_VERSION_RUN, "tool_missing", _all(replace(_TOOL_WIDE, cli_code="tool_missing"))
        ),
        "tool_too_old": _row(
            _D_VERSION_OLD, "tool_too_old", _all(replace(_TOOL_WIDE, cli_code="tool_too_old"))
        ),
    }
)

# forgectl's error codes (its pinned schemas) with a row of their own. `refused` is the
# `unknown_host` row only with that reason (decide_code), else the default row.
CODE_ROWS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "internal": "transient",
        "usage": "usage_rejected",
        "invalid_pr": "usage_rejected",
        "config_invalid": "config_invalid",
        "not_found": "not_found",
        "auth_required": "host_down",
        "permission_denied": "permission_denied",
        "rate_limited": "rate_limited",
        "upstream_error": "transient",
        "network_error": "transient",
        "tool_missing": "forgectl_tool_missing",
        "host_rejected": "host_rejected",
        "unsupported_host_version": "host_rejected",
        "wrong_host_response": "wrong_host_response",
        "refused": "transient",
    }
)
# Pinned codes that none of the five commands pr-watch calls can end with: the default row.
DEFAULTED: Final = frozenset(
    {
        "body_too_large", "method_required", "vetoed", "outcome_unknown", "outcome_partial",
        "merge_unverified", "invalid_position", "quick_action_ran", "approved_stale_head",
        "audit_unavailable", "audit_failed", "partial_results", "confirmation_required",
        "escalated",
    }
)  # fmt: skip
DEFAULT_ROW: Final = "transient"


@dataclass(frozen=True)
class Decision:
    """What one outcome means for one call kind: the row, the failure code (the one reported in
    `failed[]` and `last_failure_code`), the effect and the facts `to_cli_error` needs."""

    row: str
    call: str
    code: str | None
    effect: Effect
    retry_after: float | None = None
    error: ForgectlError | None = None
    detail: str = ""
    tool: ToolError | None = None


def _check_call(call: str) -> None:
    if call not in CALLS:
        raise ValueError(f"unknown call kind {call!r}")


def _decision(row: str, call: str, code: str | None, **facts: Any) -> Decision:
    _check_call(call)
    effect = ROWS[row].effects.get(call)
    if effect is None:
        raise ValueError(f"row {row!r} does not apply to a {call!r} call")
    return Decision(row=row, call=call, code=code, effect=effect, **facts)


def decision_for(row: str, call: str) -> Decision:
    """The decision of a named row with its default code (for tests and previews)."""
    return _decision(row, call, ROWS[row].code)


def decide_code(code: str, reason: str | None, call: str) -> Decision:
    """A forgectl error code (and refusal reason) for one call kind; unknown is the default row.

    Used for an envelope's error and for each entry of a partial result's `errors`."""
    if not isinstance(code, str) or not (reason is None or isinstance(reason, str)):
        raise TypeError("code and reason must be text")
    _check_call(call)
    if code == "refused" and reason == "unknown_host":
        row = "unknown_host"
    else:
        row = CODE_ROWS.get(code, DEFAULT_ROW)
    shown = code if _CODE.fullmatch(code) else "internal"
    if row == "not_found" and call in ("query", "whoami"):
        shown = "internal"  # the interface: `not_found` of a host call is `internal`
    return _decision(row, call, shown)


def decide_local(name: str, call: str) -> Decision:
    """A failure pr-watch itself found: `pr_id_changed` (a PR call only)."""
    if name != "pr_id_changed":
        raise ValueError(f"unknown local failure {name!r}")
    return _decision("pr_id_changed", call, "pr_id_changed")


def decide_tool(exc: ToolError) -> Decision:
    """`--version` could not run (`tool_missing`) or is wrong (`tool_too_old`): tool-wide."""
    row = "tool_too_old" if isinstance(exc, ToolTooOld) else "tool_missing"
    return _decision(row, "pr", row, tool=exc)


def decide(outcome: Outcome, call: str) -> Decision:
    """The decision for one forgectl call's `Outcome`.

    There is no "pr-watch was signalled" input: the poller checks its own signal state before it
    classifies an exit above 128 and stops instead of asking here."""
    if not isinstance(outcome, Outcome):
        raise TypeError("decide needs an Outcome")
    _check_call(call)
    if outcome.local == "too_large":
        decision = _decision("too_large", call, "too_large")
        if outcome.command != "snapshot" and decision.effect.fallback_show:
            # a too_large `show` must not request another `show`
            decision = replace(decision, effect=replace(decision.effect, fallback_show=False))
        return decision
    if outcome.local == "killed":
        return _decision("transient", call, "killed")
    if outcome.local == "contract":
        return _decision("contract", call, "contract", detail=outcome.detail)
    if outcome.local is not None:
        return _decision("transient", call, "internal")
    code = outcome.returncode
    if outcome.result is not None:
        if code == 0:
            return _decision("success", call, None)
        if call == "pr" or code is None:  # only a listing or `whoami` has a partial result
            return _decision("contract", call, "contract", detail=outcome.detail)
        return _decision("partial", call, None)
    if outcome.error is not None:
        decision = decide_code(outcome.error.code, outcome.error.reason, call)
        return replace(decision, retry_after=outcome.error.retry_after, error=outcome.error)
    if code is None or code < 0 or code == 137:
        return _decision("transient", call, "killed")
    if code > 128:
        return _decision("interrupted", call, "interrupted")
    return _decision("transient", call, "internal")  # nothing to read and no signal: a bug


# --- what the poller does with a decision ------------------------------------------------------


def host_pause(retry_after: float | None, pause_step: int) -> tuple[float, int]:
    """(seconds to pause the host, the new persisted `pause_step`) after a `rate_limited` result.

    With `retry_after`: that many seconds, at most an hour, and the step is left alone. Without:
    a minute, doubling on each further result up to 15 minutes; the step is the last such pause
    (0 after a success, which resets it)."""
    if isinstance(pause_step, bool) or not isinstance(pause_step, int) or pause_step < 0:
        raise ValueError("pause_step must be a non-negative int")
    if retry_after is not None:
        if isinstance(retry_after, bool) or not math.isfinite(retry_after) or retry_after < 0:
            raise ValueError("retry_after must be a finite non-negative number")
        return min(float(retry_after), MAX_PAUSE), pause_step
    step = max(BACKOFF_START, min(pause_step * 2, BACKOFF_MAX))
    return float(step), step


def apply_counter(
    effect: Effect, count: int, unreachable_after: int, status: str
) -> tuple[int, bool]:
    """(new `failure_count`, becomes `unreachable`) after an effect on a PR whose current status
    is `status`. A PR becomes unreachable once, when the count crosses `unreachable_after`, and
    only while it is `active`; a `terminal` or `unreachable` PR never does."""
    if effect.counter == "reset":
        return 0, False
    if effect.counter == "increment":
        new = count + 1
        return new, status == "active" and count < unreachable_after <= new
    return count, False


def should_record(
    prev_code: str | None, new_code: str | None, new_count: int, unreachable_after: int
) -> bool:
    """Whether a counted failure writes its `snapshot_failed` record (the interface's log rule):
    the first failure of a streak (`new_count` 1), a change of code, and every multiple of
    `unreachable_after`. `Effect.record` only says the failure is eligible."""
    return (
        new_count == 1
        or prev_code != new_code
        or (unreachable_after > 0 and new_count % unreachable_after == 0)
    )


# --- direct commands ----------------------------------------------------------------------------

_MESSAGES: Final = {
    "not_found": "the PR was not found on the host",
    "refused": "forgectl refused the call",
    "internal": "forgectl rejected a reference pr-watch built",
    "contract": "a forgectl result violated the documented shape",
}


def to_cli_error(decision: Decision) -> CliError:
    """The `CliError` a direct command ends with for a failed decision."""
    code = decision.effect.cli_code
    if code is None:
        raise ValueError("a success is not an error")
    error, tool = decision.error, decision.tool
    details: dict[str, Any] | None = None
    if code == "tool_missing":
        text = error.details.get("install") if error and error.details else None
        install = text if isinstance(text, str) else (tool.install if tool else INSTALL)
        details = {"install": install}
        message = tool.message if tool else "forgectl reported a missing tool"
    elif code == "tool_too_old":
        old = tool if isinstance(tool, ToolTooOld) else None
        details = {
            "required": old.required if old else "unknown",
            "found": old.found if old else "unknown",
        }
        message = str(tool) if tool else "forgectl is too old"
    elif code == "contract":
        message = decision.detail or _MESSAGES["contract"]
    elif code == "host_failed" and decision.row == "pr_id_changed":
        message = "the stored PR id differs from the PR the reference now names"
    elif code == "host_failed":
        message = f"forgectl failed ({decision.code})"
    else:
        message = _MESSAGES[code]
    lower = None
    if error is not None:
        lower = {
            "code": error.code,
            "reason": error.reason,
            "retry_after": error.retry_after,
            "message": error.message,
        }
    reason = None
    if error is not None and error.code == "refused" and isinstance(error.reason, str):
        if _CODE.fullmatch(error.reason):
            reason = error.reason
    if reason is None and code == "refused":
        reason = "unknown_host"  # the `unknown_host` row with no error to read the reason from
    if reason is not None:
        code = "refused"  # any refusal reason ends a direct command as `refused` (exit 20)
        message = _MESSAGES["refused"]
    return CliError(
        code,
        message,
        reason=reason,
        details=details,
        lower_error=lower,
    )
