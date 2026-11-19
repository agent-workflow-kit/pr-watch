"""Digest pair to ordered events (docs/interface.md, Events).

`diff(old, new, snapshot)` is a pure function: the stored digest before the snapshot, the digest
built from the snapshot, and the snapshot itself (read only for event payload fields). It returns
`DiffEvent`s in the fixed in-snapshot order; the poll turns each into a `NewEvent` inside the
commit transaction, where labels and watches are read (`DiffEvent.to_new_event`).

Rules live in `RULES`, one entry per event type, in the documented order. A later issue adds its
rules by adding entries at the marked seams; the machinery below does not change.

Conservative readings (the interface is silent):

- An event that cannot satisfy schemas/event.json is not produced rather than produced invalid
  (a stored invalid event would abort the whole commit): a `head_sha` not 40 hex, a malformed
  repo, `pr` or `pr_id`, a comment or review id outside forgectl's id forms, a review state
  outside the schema's enum.
- A null or unknown value is never a change: an unreadable state, head or reviewer is no event.
- `merged` is final: nothing is emitted for a PR whose stored state is `merged`.
- A part that is not baselined (or any part under `rebaseline`) is silent.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from .digest import instant
from .store import PR_ID_RE, PR_RE, REPO_RE, SHA_RE, NewEvent, format_ts
from .text import bounded_one_line

MAX_TEXT: Final = 256
_FORGECTL_ID: Final = re.compile(
    r"gh:(?:ic|rc|rt|r):[A-Za-z0-9_=-]{1,100}|gl:d:[0-9a-f]{40}|gl:n:[1-9][0-9]{0,18}"
)
_ACCOUNT_ID: Final = re.compile(r"[A-Za-z0-9:/_=-]{1,128}")
_SIDE: Final = re.compile(r"[A-Za-z_]{1,16}")
_REVIEW_STATES: Final = ("approved", "changes_requested", "commented", "dismissed")
_LAST: Final = datetime.max.replace(tzinfo=UTC)

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
class Ctx:
    old: Mapping[str, Any]
    new: Mapping[str, Any]
    snap: Mapping[str, Any]
    rebaseline: bool
    forced: ForcedCheck | None


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


def _review(ctx: Ctx) -> list[dict[str, Any]]:
    if _quiet(ctx, "reviews"):
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
        if not _FORGECTL_ID.fullmatch(rid) or item.get("state") not in _REVIEW_STATES:
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
        if item.get("minimized") is True or not _FORGECTL_ID.fullmatch(cid):
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
                line=line
                if isinstance(line, int) and not isinstance(line, bool) and line >= 0
                else None,
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
    return ctx.new.get("state") == state and ctx.old.get("state") in from_states


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
    if not _became(ctx, "closed", ("open",)):
        return []
    return [{"occurred_at": _ts(ctx.snap.get("closed_at"))}]


# The documented in-snapshot order. TODO(#15): add approved, changes_requested (after
# review_comment), ci and ready (before merged); TODO(#16): reopen (a closed PR that is open
# again is a silent re-baseline except pushed), rename by pr_id, incomplete-part counters.
RULES: Final[tuple[Rule, ...]] = (
    Rule("pushed", _pushed),
    Rule("review", _review),
    Rule("comment", _comment),
    Rule("review_comment", _review_comment),
    Rule("merged", _merged, on_first_sight=True),
    Rule("closed", _closed, on_first_sight=True),
)


# --- entry point -------------------------------------------------------------------------------


def diff(
    old: Mapping[str, Any] | None,
    new: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    rebaseline: bool = False,
    forced: ForcedCheck | None = None,
) -> list[DiffEvent]:
    """The ordered events of one snapshot.

    `old` is the stored digest (null or malformed: first sight, which emits only the terminal
    event of a PR already merged or closed). `forced(from_sha, to_sha)` is called only for a push
    and returns `forced` for the event (see `forced_from_compare`); an exception it raises is the
    caller's to handle.
    """
    snap = _map(snapshot)
    repo, pr, pr_id, head = (
        snap.get("repo"),
        snap.get("pr"),
        snap.get("pr_id"),
        _sha(new.get("head_sha")),
    )
    if not (
        isinstance(repo, str) and REPO_RE.match(repo)
        and isinstance(pr, str) and len(pr) <= 256 and PR_RE.match(pr)
        and isinstance(pr_id, str) and PR_ID_RE.match(pr_id)
        and head is not None
    ):  # fmt: skip
        return []
    stored = old if isinstance(old, Mapping) and "state" in old else None
    first = stored is None
    if stored is not None and stored.get("state") == "merged":
        return []
    # first sight compares the terminal rules against an assumed `open` before the PR was seen
    ctx = Ctx(stored if stored is not None else {"state": "open"}, new, snap, rebaseline, forced)
    events: list[DiffEvent] = []
    for rule in RULES:
        if first and not rule.on_first_sight:
            continue
        for extra in rule.fire(ctx):
            payload = {"repo": repo, "head_sha": head, **extra}
            events.append(DiffEvent(rule.type, pr_id, pr, payload))
    return events
