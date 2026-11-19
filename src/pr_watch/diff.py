"""Digest pair to ordered events (docs/interface.md, Events).

`diff(old, new, snapshot)` is a pure function: the stored digest before the snapshot, the digest
built from the snapshot, and the snapshot itself (read only for event payload fields). It returns
`DiffEvent`s in the fixed in-snapshot order; the poll turns each into a `NewEvent` inside the
commit transaction, where labels and watches are read (`DiffEvent.to_new_event`).

Rules live in `RULES`, one entry per event type, in the documented order. A later issue adds its
rules by adding entries at the marked seams; the machinery below does not change.

Conservative readings (the interface is silent):

- An event that cannot satisfy schemas/event.json is not produced rather than produced invalid
  (a stored invalid event would abort the whole commit), and is never dropped silently: `diff`
  returns each drop in `DiffResult.dropped` as a `Drop(type, reason)` of fixed reason codes (no
  host text) for the poller to log. Reasons: `invalid_identity` (a malformed repo, `pr` or
  `pr_id`), `invalid_head_sha` (no valid head, below), `invalid_comment_id` and
  `invalid_review_id` (outside forgectl's id forms), `invalid_review_state` (outside the
  schema's enum).
- The common `head_sha` is the new digest's head when valid, else the stored digest's head;
  with neither valid the events other than `pushed` are dropped (`invalid_head_sha`). `pushed`
  needs both heads valid and is otherwise suppressed (a null or unknown value is not a change).
- A null or unknown value is never a change: an unreadable state, head or reviewer is no event.
- A stored `state` that is null (never readable) counts as first sight for `merged` and `closed`.
- `review` is emitted for GitHub-backed PRs only (the snapshot url is a `pull` url).
- `merged` is final: nothing is emitted for a PR whose stored state is `merged`.
- A part that is not baselined (or any part under `rebaseline`) is silent; `ready` has no part
  and is silent under `rebaseline` only. A silent `ci` is adopted as the stored state with the
  hold cleared.
- A reviewer whose new state is `unknown` is no `approved` or `changes_requested`; a stored
  `unknown` counts as no entry (a later known verdict fires); a reviewer new to the digest is
  `not approved`. `approved` compares `at` by instant (raw text only when neither parses).
- A `reviewer` is `{id, login, is_bot}` with `id` the reviewer key (null for a `login:` key). All
  three come from the latest review by the key's author id; else `login` is the original-case
  login of the `latest_by_reviewer` item for the key (null for a hashed overlong key), `is_bot`
  null (GitLab has no reviews).
- A `ci` event whose `for_head` (forgectl's `ci.head_sha`) is null is dropped with
  `invalid_head_sha`: the schema needs a sha and one is never invented. The held state is still
  committed.

The CI hold is applied here: `DiffResult.digest` is the new digest with its `ci` replaced by the
held one, and is what the poll stores (the digests passed in are never modified). `settle_polls` is
the repo's `ci.settle_polls`.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlsplit

from .digest import (
    counted_checks,
    incomplete_parts,
    instant,
    reviewer_key,
    winning_reviewer_item,
)
from .store import MAX_SAFE_INT, PR_ID_RE, PR_RE, REPO_RE, SHA_RE, NewEvent, format_ts
from .text import bounded_one_line

MAX_TEXT: Final = 256
_FORGECTL_ID: Final = re.compile(
    r"gh:(?:ic|rc|rt|r):[A-Za-z0-9_=-]{1,100}|gl:d:[0-9a-f]{40}|gl:n:[1-9][0-9]{0,18}"
)
_ACCOUNT_ID: Final = re.compile(r"[A-Za-z0-9:/_=-]{1,128}")
_SIDE: Final = re.compile(r"[A-Za-z_]{1,16}")
_REVIEW_STATES: Final = ("approved", "changes_requested", "commented", "dismissed")
_LAST: Final = datetime.max.replace(tzinfo=UTC)
_FIRST: Final = datetime.min.replace(tzinfo=UTC)
_STATES: Final = ("open", "closed", "merged")
_CI_STATES: Final = ("passing", "failing", "pending", "none")
_BLOCK_CODE: Final = re.compile(r"[a-z_]{1,64}")
_CHECK_STATE: Final = re.compile(r"[a-z_]{1,32}")
MAX_CHECK_CHANGES: Final = 20

# forgectl `compare` statuses (interface, Events): True for a rewritten history.
_FORCED: Final = {"diverged": True, "behind": True, "unavailable": True, "ahead": False}

ForcedCheck = Callable[[str, str], bool | None]


def forced_from_compare(status: object) -> bool | None:
    """`forced` from a forgectl `compare` status: `diverged`, `behind` and `unavailable` are
    true, `ahead` is false, anything else (`identical`, `too_large`, a failed call) is null."""
    return _FORCED.get(status) if isinstance(status, str) else None


@dataclass(frozen=True)
class DiffEvent:
    """One detected difference. `payload` holds every schema field that is not a column, except
    `watches` (read inside the commit transaction)."""

    type: str
    pr_id: str
    pr: str
    payload: Mapping[str, Any]

    def to_new_event(self, labels: Sequence[str], watches: Sequence[str]) -> NewEvent:
        return NewEvent(
            type=self.type,
            pr_id=self.pr_id,
            pr=self.pr,
            payload={**self.payload, "watches": list(watches)},
            labels=tuple(labels),
        )


@dataclass(frozen=True)
class Drop:
    """A detected event that cannot be stored. `reason` is a fixed code, never host text."""

    type: str
    reason: str


class DiffResult(list[DiffEvent]):
    """The ordered events, a plain list for callers that only want events, plus the drops."""

    dropped: tuple[Drop, ...] = ()

    def __init__(self, events: Sequence[DiffEvent] = ()) -> None:
        super().__init__(events)
        self.digest: dict[str, Any] = {}


@dataclass(frozen=True)
class CiPlan:
    """What the new snapshot does to the stored `ci`: the `ci` to store, and whether the state
    change fires (`fire` is only ever true for a committed change)."""

    ci: Mapping[str, Any]
    fire: bool


@dataclass(frozen=True)
class Ctx:
    old: Mapping[str, Any]
    new: Mapping[str, Any]
    snap: Mapping[str, Any]
    rebaseline: bool
    forced: ForcedCheck | None
    drops: list[Drop]
    ci_plan: CiPlan


@dataclass(frozen=True)
class Rule:
    """`fire` returns the payload extras of each event (type-specific fields plus `occurred_at`);
    the common fields are added by `diff`. `on_first_sight` rules also run with no stored digest."""

    type: str
    fire: Callable[[Ctx], list[dict[str, Any]]]
    on_first_sight: bool = False


# --- small readers -----------------------------------------------------------------------------


def _map(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sha(value: object) -> str | None:
    return value if isinstance(value, str) and SHA_RE.match(value) else None


def _ts(value: object) -> str | None:
    moment = instant(value)
    return format_ts(moment) if moment is not None else None


def _text(value: object) -> str | None:
    return bounded_one_line(value, MAX_TEXT) if isinstance(value, str) else None


def _account(value: object) -> dict[str, Any]:
    item = _map(value)
    ident, bot = item.get("id"), item.get("is_bot")
    return {
        "id": ident if isinstance(ident, str) and _ACCOUNT_ID.fullmatch(ident) else None,
        "login": _text(item.get("login")),
        "is_bot": bot if isinstance(bot, bool) else None,
    }


def _line(value: object) -> int | None:
    """A source line: an int (not a bool) in 0 to the store's integer limit, else null."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_SAFE_INT:
        return value
    return None


def _quiet(ctx: Ctx, part: str) -> bool:
    """A part is silent under `rebaseline`, and until the stored digest has it baselined."""
    return ctx.rebaseline or _map(ctx.old.get("baselined")).get(part) is not True


def _fresh_ids(ctx: Ctx, key: str) -> set[str]:
    """Ids in the new digest's `seen[key]` and not in the old one's. An incomplete part keeps its
    old list, so it yields nothing."""

    def ids(digest: Mapping[str, Any]) -> set[str]:
        items = _map(digest.get("seen")).get(key)
        return {i for i in items if isinstance(i, str)} if isinstance(items, list) else set()

    return ids(ctx.new) - ids(ctx.old)


def _order(item: Mapping[str, Any], when: object) -> tuple[datetime, str]:
    return (instant(when) or _LAST, str(item.get("id")))


# --- rules -------------------------------------------------------------------------------------


def _pushed(ctx: Ctx) -> list[dict[str, Any]]:
    before, after = _sha(ctx.old.get("head_sha")), _sha(ctx.new.get("head_sha"))
    if before is None or after is None or before == after:
        return []
    forced = ctx.forced(before, after) if ctx.forced is not None else None
    return [
        {
            "occurred_at": _ts(_map(ctx.snap.get("head")).get("pushed_at")),
            "from_sha": before,
            "to_sha": after,
            "forced": forced if isinstance(forced, bool) else None,
        }
    ]


def _github_backed(snap: Mapping[str, Any]) -> bool:
    """False only for a snapshot whose url positionally names a GitLab merge request.

    A GitLab url ends `/merge_requests/<n>`; any other shape, or a missing url, is treated as
    GitHub so a lost url never suppresses `review` (GitLab has no `gh:r:` review ids).
    """
    url = snap.get("url")
    if not isinstance(url, str):
        return True
    parts = urlsplit(url).path.split("/")
    return not (len(parts) >= 2 and parts[-2] == "merge_requests")


def _review(ctx: Ctx) -> list[dict[str, Any]]:
    if _quiet(ctx, "reviews") or not _github_backed(ctx.snap):
        return []
    fresh = _fresh_ids(ctx, "reviews")
    reviews = ctx.snap.get("reviews")
    picked: dict[str, Mapping[str, Any]] = {}
    for item in reviews if isinstance(reviews, list) else ():
        rid = _map(item).get("id")
        if isinstance(rid, str) and rid in fresh and rid not in picked:
            picked[rid] = _map(item)
    out = []
    for rid, item in sorted(
        picked.items(), key=lambda kv: _order(kv[1], kv[1].get("submitted_at"))
    ):
        if not _FORGECTL_ID.fullmatch(rid):
            ctx.drops.append(Drop("review", "invalid_review_id"))
            continue
        if item.get("state") not in _REVIEW_STATES:
            ctx.drops.append(Drop("review", "invalid_review_state"))
            continue
        out.append(
            {
                "occurred_at": _ts(item.get("submitted_at")),
                "review_id": rid,
                "author": _account(item.get("author")),
                "review_state": item["state"],
            }
        )
    return out


def _comment_kind(comment_id: str, thread: Mapping[str, Any]) -> str | None:
    """`comment` or `review_comment` by the id form; a GitLab note by its discussion position."""
    if comment_id.startswith("gh:ic:"):
        return "comment"
    if comment_id.startswith("gh:rc:"):
        return "review_comment"
    if comment_id.startswith("gl:n:"):
        return "review_comment" if isinstance(thread.get("path"), str) else "comment"
    return None


def _comments(ctx: Ctx, kind: str) -> list[dict[str, Any]]:
    if _quiet(ctx, "threads"):
        return []
    fresh = _fresh_ids(ctx, "comments")
    picked: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
    threads = ctx.snap.get("threads")
    for thread in threads if isinstance(threads, list) else ():
        comments = _map(thread).get("comments")
        for item in comments if isinstance(comments, list) else ():
            cid = _map(item).get("id")
            if isinstance(cid, str) and cid in fresh and cid not in picked:
                picked[cid] = (_map(thread), _map(item))
    out = []
    for cid, (thread, item) in sorted(
        picked.items(), key=lambda kv: _order(kv[1][1], kv[1][1].get("created_at"))
    ):
        if item.get("minimized") is True:
            continue
        if not _FORGECTL_ID.fullmatch(cid):
            # the kind of an unreadable id is guessed from its prefix
            if ("review_comment" if cid.startswith("gh:rc:") else "comment") == kind:
                ctx.drops.append(Drop(kind, "invalid_comment_id"))
            continue
        if _comment_kind(cid, thread) != kind:
            continue
        tid = thread.get("id")
        thread_id = (
            tid
            if not cid.startswith("gh:ic:") and isinstance(tid, str) and _FORGECTL_ID.fullmatch(tid)
            else None
        )
        extra: dict[str, Any] = {
            "occurred_at": _ts(item.get("created_at")),
            "comment_id": cid,
            "thread_id": thread_id,
            "author": _account(item.get("author")),
        }
        if kind == "review_comment":
            line, side = thread.get("line"), thread.get("side")
            extra.update(
                path=_text(thread.get("path")) or "",
                line=_line(line),
                side=side if isinstance(side, str) and _SIDE.fullmatch(side) else None,
                commit_sha=_sha(thread.get("commit_sha")),
            )
        out.append(extra)
    return out


def _comment(ctx: Ctx) -> list[dict[str, Any]]:
    return _comments(ctx, "comment")


def _review_comment(ctx: Ctx) -> list[dict[str, Any]]:
    return _comments(ctx, "review_comment")


def _became(ctx: Ctx, state: str, from_states: tuple[str, ...]) -> bool:
    # a stored state that was never readable (null) counts as `open`, like first sight
    before = ctx.old.get("state")
    effective = before if before in _STATES else "open"
    return ctx.new.get("state") == state and effective in from_states


def _merged(ctx: Ctx) -> list[dict[str, Any]]:
    if not _became(ctx, "merged", ("open", "closed")):
        return []
    return [
        {
            "occurred_at": _ts(ctx.snap.get("merged_at")),
            "merged_head_sha": _sha(ctx.new.get("merged_head_sha")),
            "merge_commit_sha": _sha(ctx.new.get("merge_commit_sha")),
        }
    ]


def _closed(ctx: Ctx) -> list[dict[str, Any]]:
    # a reactivated row (`rebaseline`) stored `closed` and closed again before this snapshot: the
    # reopen was never seen, but the consumer has not seen this closing either, so it fires once
    again = ctx.rebaseline and ctx.old.get("state") == "closed" and ctx.new.get("state") == "closed"
    if not again and not _became(ctx, "closed", ("open",)):
        return []
    return [{"occurred_at": _ts(ctx.snap.get("closed_at"))}]


def _reviewer_entries(digest: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    return {k: _map(v) for k, v in _map(digest.get("reviewers")).items() if isinstance(k, str)}


def _reviewer_account(ctx: Ctx, key: str) -> dict[str, Any]:
    """`{id, login, is_bot}` of reviewer `key`. A review in the snapshot whose author id is the
    key gives all three (the latest by `submitted_at`; an unparseable one ranks lowest). Else the
    `latest_by_reviewer` item the digest builder kept for the key gives the login (original case,
    sanitized; `is_bot` stays null); the id is the key unless the item is login-keyed, and a
    hashed overlong `login:#` key has no login."""
    found: list[tuple[datetime, str, Mapping[str, Any]]] = []
    reviews = ctx.snap.get("reviews")
    for item in reviews if isinstance(reviews, list) else ():
        who = _map(_map(item).get("author"))
        if who.get("id") == key:
            found.append(
                (instant(_map(item).get("submitted_at")) or _FIRST, str(_map(item).get("id")), who)
            )
    if found:
        account = _account(max(found, key=lambda f: (f[0], f[1]))[2])
        return {"id": key, "login": account["login"], "is_bot": account["is_bot"]}
    item = winning_reviewer_item(ctx.snap, key)
    if item is None:
        return {"id": None if key.startswith("login:") else key, "login": None, "is_bot": None}
    if reviewer_key({"id": item.get("id")}) == key:  # keyed by a valid id
        return {"id": key, "login": _text(item.get("login")), "is_bot": None}
    login = None if key.startswith("login:#") else _text(item.get("login"))
    return {"id": None, "login": login, "is_bot": None}


def _same_at(was: object, now: object) -> bool:
    """Equal instants (any offset or fraction form); raw equality when neither parses."""
    before, after = instant(was), instant(now)
    if before is not None and after is not None:
        return before == after
    return before is None and after is None and was == now


def _verdicts(ctx: Ctx, state: str) -> list[dict[str, Any]]:
    """The reviewers whose entry is `state` and is new: no stored `approved`/`changes_requested`
    entry of that state, or a different `at`, or (both `at` null) a different `commit_sha`."""
    if _quiet(ctx, "reviews"):
        return []
    before, after = _reviewer_entries(ctx.old), _reviewer_entries(ctx.new)
    out = []
    for key in sorted(after):
        now, was = after[key], before.get(key)
        if now.get("state") != state:
            continue
        if was is not None:
            if was.get("state") == state and _same_at(was.get("at"), now.get("at")):
                if now.get("at") is not None or was.get("commit_sha") == now.get("commit_sha"):
                    continue
        at = _ts(now.get("at"))
        out.append(
            {
                "occurred_at": at,
                "reviewer": _reviewer_account(ctx, key),
                "commit_sha": _sha(now.get("commit_sha")),
                "at": at,
                "can_push": now["can_push"] if isinstance(now.get("can_push"), bool) else None,
            }
        )
    return out


def _approved(ctx: Ctx) -> list[dict[str, Any]]:
    approvals = ctx.new.get("approvals_at_head")
    count = approvals if isinstance(approvals, int) and not isinstance(approvals, bool) else None
    return [{**extra, "approvals_at_head": count} for extra in _verdicts(ctx, "approved")]


def _changes_requested(ctx: Ctx) -> list[dict[str, Any]]:
    return _verdicts(ctx, "changes_requested")


def _check_changes(old: object, new: object) -> list[dict[str, Any]]:
    """Checks whose key appears, disappears or changes state, ordered by (name, key)."""
    before, after = _map(old), _map(new)
    out: list[tuple[str, str, dict[str, Any]]] = []
    for key in sorted(set(before) | set(after)):
        was, now = _map(before.get(key)), _map(after.get(key))
        was_state, now_state = was.get("state"), now.get("state")
        if was_state == now_state:
            continue
        name = _text(now.get("name") if now else was.get("name")) or ""
        out.append(
            (
                name,
                key,
                {
                    "name": name,
                    "from": was_state
                    if isinstance(was_state, str) and _CHECK_STATE.fullmatch(was_state)
                    else None,
                    "to": now_state
                    if isinstance(now_state, str) and _CHECK_STATE.fullmatch(now_state)
                    else None,
                },
            )
        )
    return [item for _, _, item in sorted(out, key=lambda o: (o[0], o[1]))]


def _ci_counts(snap: Mapping[str, Any]) -> dict[str, int]:
    """Counted checks of the snapshot (merged by key, worst state wins) by state."""
    counts = {"passing": 0, "failing": 0, "pending": 0}
    for _name, state in counted_checks(_map(snap.get("ci"))).values():
        if state in counts:
            counts[state] += 1
    return counts


def _ci(ctx: Ctx) -> list[dict[str, Any]]:
    if not ctx.ci_plan.fire:
        return []
    old_ci, new_ci = _map(ctx.old.get("ci")), _map(ctx.new.get("ci"))
    changes = _check_changes(old_ci.get("checks"), new_ci.get("checks"))
    for_head = _sha(new_ci.get("head_sha"))
    if for_head is None:
        ctx.drops.append(Drop("ci", "invalid_head_sha"))
        return []
    extra: dict[str, Any] = {
        "occurred_at": None,
        "from": old_ci.get("state"),
        "to": new_ci.get("state"),
        "checks": changes[:MAX_CHECK_CHANGES],
        "counts": _ci_counts(ctx.snap),
        "for_head": for_head,
    }
    if len(changes) > MAX_CHECK_CHANGES:
        extra["checks_more"] = len(changes) - MAX_CHECK_CHANGES
    return [extra]


def _ready(ctx: Ctx) -> list[dict[str, Any]]:
    if ctx.rebaseline or ctx.old.get("draft") is not True or ctx.new.get("draft") is not False:
        return []
    return [
        {
            "occurred_at": None,
            "from_blocked_by": _blocked(ctx.old),
            "blocked_by": _blocked(ctx.new),
        }
    ]


def _blocked(digest: Mapping[str, Any]) -> list[Any]:
    value = digest.get("blocked_by")
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        code = _map(item).get("code")
        if not isinstance(code, str) or not _BLOCK_CODE.fullmatch(code):
            continue
        if code != "other":
            out.append({"code": code})
            continue
        detail = _map(item).get("detail")
        if isinstance(detail, str) and bounded_one_line(detail, MAX_TEXT) == detail:
            out.append({"code": code, "detail": detail})
        elif "detail" not in item:
            out.append({"code": code})
    return out


def plan_ci(
    old: Mapping[str, Any],
    new: Mapping[str, Any],
    snap: Mapping[str, Any],
    *,
    quiet: bool,
    settle_polls: int,
) -> CiPlan:
    """The CI hold (interface, Events). Compares the stored `ci.state` with the one the snapshot
    reports. A change between pending, passing and failing commits and fires. A change involving
    `none` is held, counting consecutive snapshots that reported the same new state, and commits
    and fires at the snapshot where the count exceeds `settle_polls`; the held `ci` keeps the
    stored state, head and checks. Any snapshot that reports the stored state, or another state,
    resets the count. An incomplete `ci` freezes everything, the count included. A silent `ci`
    (not baselined, or under rebaseline) is adopted with the count cleared."""
    new_ci = _map(new.get("ci"))
    stored = _map(old.get("ci"))
    if "ci" in incomplete_parts(snap):
        return CiPlan(new_ci, False)
    before, after = stored.get("state"), new_ci.get("state")
    if quiet or before not in _CI_STATES or after not in _CI_STATES:
        return CiPlan(new_ci, False)
    if before == after:
        return CiPlan(new_ci, False)
    if before != "none" and after != "none":
        return CiPlan(new_ci, True)
    carried = stored.get("hold") if stored.get("hold_to") == after else 0
    held = carried if isinstance(carried, int) and not isinstance(carried, bool) else 0
    count = max(0, held) + 1
    if count > settle_polls:
        return CiPlan(new_ci, True)
    return CiPlan({**copy.deepcopy(dict(stored)), "hold": count, "hold_to": after}, False)


# The documented in-snapshot order.
RULES: Final[tuple[Rule, ...]] = (
    Rule("pushed", _pushed),
    Rule("review", _review),
    Rule("comment", _comment),
    Rule("review_comment", _review_comment),
    Rule("approved", _approved),
    Rule("changes_requested", _changes_requested),
    Rule("ci", _ci),
    Rule("ready", _ready),
    Rule("merged", _merged, on_first_sight=True),
    Rule("closed", _closed, on_first_sight=True),
)


# --- entry point -------------------------------------------------------------------------------


def _reopened(stored: Mapping[str, Any] | None, new: Mapping[str, Any]) -> bool:
    """A stored `closed` that the snapshot shows `open` again: a reopen, with or without the
    row's `rebaseline` flag (a reactivation sets it; this covers a row that missed it)."""
    return stored is not None and stored.get("state") == "closed" and new.get("state") == "open"


def diff(
    old: Mapping[str, Any] | None,
    new: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    rebaseline: bool = False,
    forced: ForcedCheck | None = None,
    settle_polls: int = 1,
) -> DiffResult:
    """The ordered events of one snapshot, with the drops in `.dropped`.

    `old` is the stored digest (null or malformed: first sight, which emits only the terminal
    event of a PR already merged or closed). `forced(from_sha, to_sha)` is called only for a push
    and returns `forced` for the event (see `forced_from_compare`); an exception it raises is the
    caller's to handle. `settle_polls` is the repo's `ci.settle_polls` (0 means no hold). The
    digest to store is `result.digest`: `new` with the CI hold applied (a copy; inputs are never
    modified).
    """
    snap = _map(snapshot)
    repo, pr, pr_id = snap.get("repo"), snap.get("pr"), snap.get("pr_id")
    identity_ok = bool(
        isinstance(repo, str) and REPO_RE.match(repo)
        and isinstance(pr, str) and len(pr) <= 256 and PR_RE.match(pr)
        and isinstance(pr_id, str) and PR_ID_RE.match(pr_id)
    )  # fmt: skip
    stored = old if isinstance(old, Mapping) and "state" in old else None
    first = stored is None
    result = DiffResult()
    result.digest = copy.deepcopy(dict(new))
    if _reopened(stored, new):
        # a reopen is a silent re-baseline: parts that stay incomplete must not keep a baseline
        rebaseline = True
        flags = dict(_map(result.digest.get("baselined")))
        for part in incomplete_parts(snap):
            flags[part] = False
        result.digest["baselined"] = flags
    if stored is not None and stored.get("state") == "merged":
        return result
    head = _sha(new.get("head_sha")) or (_sha(stored.get("head_sha")) if stored else None)
    # first sight compares the terminal rules against an assumed `open` before the PR was seen
    drops: list[Drop] = []
    before = stored if stored is not None else {"state": "open"}
    settle = (
        settle_polls if isinstance(settle_polls, int) and not isinstance(settle_polls, bool) else 1
    )
    quiet = first or rebaseline or _map(before.get("baselined")).get("ci") is not True
    plan = plan_ci(before, new, snap, quiet=quiet, settle_polls=max(0, settle))
    result.digest["ci"] = copy.deepcopy(dict(plan.ci))
    ctx = Ctx(before, new, snap, rebaseline, forced, drops, plan)
    found: list[DiffEvent] = []
    for rule in RULES:
        if first and not rule.on_first_sight:
            continue
        for extra in rule.fire(ctx):
            if not identity_ok:
                drops.append(Drop(rule.type, "invalid_identity"))
            elif head is None and rule.type != "pushed":
                drops.append(Drop(rule.type, "invalid_head_sha"))
            else:
                payload = {"repo": repo, "head_sha": head, **extra}
                found.append(DiffEvent(rule.type, str(pr_id), str(pr), payload))
    result.extend(found)
    result.dropped = tuple(drops)
    return result
