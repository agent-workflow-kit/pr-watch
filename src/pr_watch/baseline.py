"""One snapshot against one stored row: events, digest and the columns to commit
(docs/interface.md: Digest, Events, PR status, State transaction 2).

`plan_snapshot(row, snapshot, now=...)` is pure. It builds the digest (`build_digest`), diffs it
(`diff`) and derives every `prs` column the snapshot commit writes, so the poller only passes
`plan.fields` to `commit_snapshot` and turns `plan.events` into `NewEvent`s there. The rules owned
here:

- **Incomplete counters.** `incomplete` maps each part that is incomplete in this snapshot
  (`incomplete_parts`, the authoritative set) to the number of consecutive snapshots it has been;
  a part that is complete drops out. A stored map that is malformed counts from zero.
  `persistent` lists the parts whose count just reached `persistent_after` (the poller logs one
  `part_incomplete` record for each; none once the resulting state is terminal). `persistent_after`
  is required: the repo's `failures.unreachable_after` (default 5); a bool, non-int or value < 1
  raises ValueError. `persistent` fires only on exact equality and is suppressed for terminal
  results, so a part that crosses the threshold while the PR is closed is not reported after a
  later reopen (known, accepted).
- **Baselines.** The digest's per-part `baselined` flags are `build_digest`'s; `baselined_at` is set
  to `now` when this snapshot baselined a part that was not baselined (first sight, a late part
  baseline) or silently re-baselined the row (`rebaseline`, or a reopen); otherwise it is kept.
  `rebaseline` is always cleared.
- **Merged is final.** When the stored digest says `merged`, the plan emits nothing and keeps the
  stored digest, status and terminal columns; only the counters, `rebaseline`, `last_polled_at`
  and `failure_count` change.
- **Reopen.** A reactivated row (flag) or a stored `closed` shown `open` is re-baselined silently
  by `diff` (only `reopened`, `pushed`, `merged` and `closed` fire).
- **Opened.** `row.opened_since` (a hint the poll sets for a first-seen query member) lets `diff`
  emit `opened` at first sight; it is ignored when the row has a stored digest.
- **Rename.** A row is keyed on `pr_id`: `pr` and `repo` are refreshed from the snapshot only
  when both are well formed (at most 256 characters), `pr` is `repo#N`, and the host equals the
  row's host; otherwise both stored values are kept. Events carry the snapshot's `pr`.
  A snapshot with another `pr_id` is refused (`PrIdMismatch`: the caller runs the `pr_id_changed`
  failure path and commits nothing). A snapshot that is not a mapping raises `MalformedSnapshot`
  (a contract failure for the poller to classify).
- **Status.** A readable `merged` or `closed` makes the row `terminal` (`terminal_at` is `now`
  unless the row already was terminal of that kind), `open` makes it `active` with the terminal
  columns cleared; an unreadable state keeps the row's, except that an `unreachable` row becomes
  `active`. A success clears `failure_count`.

`snap_updated_at` and `unsettled_since` are `settle.snapshot_columns` (#22). The `watch add`
baseline and `--no-baseline` are #18, which calls this with a row that has no digest.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from .diff import DiffResult, ForcedCheck, diff
from .digest import PARTS, build_digest, digest_from_json, digest_to_json, incomplete_parts
from .store import PR_RE, REPO_RE, SHA_RE, format_ts

MAX_COUNT: Final = 2**31
_MAX_REF: Final = 256


class PrIdMismatch(ValueError):
    """The snapshot is of another PR than the row (a reused name): nothing may be committed."""


class MalformedSnapshot(ValueError):
    """The snapshot is not a mapping: a contract failure for the poller; nothing is committed."""


@dataclass(frozen=True)
class RowState:
    """The `prs` columns a snapshot plan reads. `digest` and `incomplete` are the stored JSON."""

    pr_id: str
    pr: str
    repo: str
    digest: str | None = None
    status: str = "active"
    terminal_at: str | None = None
    terminal_kind: str | None = None
    rebaseline: bool = False
    incomplete: str = "{}"
    baselined_at: str | None = None
    # Not a column: the creation time of the oldest query watch that holds the row, set by the
    # poll for a first-seen member (`opened`, docs/interface.md); None for everything else.
    opened_since: str | None = None


@dataclass(frozen=True)
class SnapshotPlan:
    events: DiffResult
    digest: dict[str, Any]
    fields: dict[str, Any]
    incomplete: dict[str, int]
    persistent: tuple[str, ...] = field(default=())


def _counters(text: str) -> dict[str, int]:
    try:
        raw = json.loads(text)
    except (TypeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        part: count
        for part, count in raw.items()
        if part in PARTS
        and isinstance(count, int)
        and not isinstance(count, bool)
        and 0 <= count < MAX_COUNT
    }


def _ref(value: object, pattern: Any) -> str | None:
    ok = isinstance(value, str) and len(value) <= _MAX_REF and pattern.match(value)
    return value if ok and isinstance(value, str) else None


def _host(pr: str) -> str:
    return pr.split("/", 1)[0]


def _refreshed(row: RowState, snap: Mapping[str, Any]) -> tuple[str, str] | None:
    """The snapshot's `pr` and `repo` if both are valid, agree with each other and keep the host."""
    pr, repo = _ref(snap.get("pr"), PR_RE), _ref(snap.get("repo"), REPO_RE)
    if pr is None or repo is None or not pr.startswith(repo + "#"):
        return None
    if _host(pr) != _host(row.pr):
        return None
    return pr, repo


def _newly_baselined(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> bool:
    before = old.get("baselined") if old is not None else None
    was = before if isinstance(before, Mapping) else {}
    now = new.get("baselined")
    return isinstance(now, Mapping) and any(
        now.get(p) is True and was.get(p) is not True for p in PARTS
    )


def plan_snapshot(
    row: RowState,
    snapshot: Mapping[str, Any],
    *,
    now: datetime,
    forced: ForcedCheck | None = None,
    settle_polls: int = 1,
    persistent_after: int,
) -> SnapshotPlan:
    if (
        isinstance(persistent_after, bool)
        or not isinstance(persistent_after, int)
        or persistent_after < 1
    ):
        raise ValueError("persistent_after must be an int >= 1 (failures.unreachable_after)")
    if not isinstance(snapshot, Mapping):
        raise MalformedSnapshot(type(snapshot).__name__)
    snap = snapshot
    if snap.get("pr_id") != row.pr_id:
        raise PrIdMismatch(row.pr_id)
    stored = digest_from_json(row.digest)
    stamp = format_ts(now)
    reopened = stored is not None and stored.get("state") == "closed"
    new = build_digest(snap, stored, rebaseline=row.rebaseline)
    result = diff(stored, new, snap, rebaseline=row.rebaseline, forced=forced,
                  settle_polls=settle_polls, opened_since=row.opened_since)  # fmt: skip
    digest = result.digest
    if stored is not None and stored.get("state") == "merged":
        # merged is final at row level: keep the stored digest and status columns
        kept = _counters(row.incomplete)
        counts = {p: kept.get(p, 0) + 1 for p in sorted(incomplete_parts(snap))}
        fields = {
            "digest": row.digest,
            "incomplete": json.dumps(counts, sort_keys=True, separators=(",", ":")),
            "rebaseline": 0,
            "failure_count": 0,
            "last_polled_at": stamp,
        }
        return SnapshotPlan(result, stored, fields, counts, ())
    reopened = reopened and digest.get("state") == "open"
    silent_rebaseline = stored is not None and (row.rebaseline or reopened)

    before = _counters(row.incomplete)
    counts = {p: before.get(p, 0) + 1 for p in sorted(incomplete_parts(snap))}
    state = digest.get("state")
    persistent = (
        ()
        if state in ("merged", "closed")
        else tuple(p for p, c in counts.items() if c == persistent_after)
    )

    fields: dict[str, Any] = {
        "digest": digest_to_json(digest),
        "incomplete": json.dumps(counts, sort_keys=True, separators=(",", ":")),
        "rebaseline": 0,
        "failure_count": 0,
        "last_polled_at": stamp,
    }
    refs = _refreshed(row, snap)
    if refs is not None:
        fields["pr"], fields["repo"] = refs
    if silent_rebaseline or _newly_baselined(stored, digest):
        fields["baselined_at"] = stamp
    if state in ("merged", "closed"):
        same = row.status == "terminal" and row.terminal_kind == state and row.terminal_at
        fields.update(
            status="terminal", terminal_kind=state, terminal_at=row.terminal_at if same else stamp
        )
    elif state == "open":
        fields.update(status="active", terminal_kind=None, terminal_at=None)
    elif row.status == "unreachable":
        fields["status"] = "active"  # a successful snapshot ends unreachable
    return SnapshotPlan(result, digest, fields, counts, persistent)


def plan_overcap(row: RowState, show: Mapping[str, Any], *, now: datetime) -> SnapshotPlan | None:
    """The plan of the over-cap fallback (docs/interface.md: Failures, "Over the output cap"):
    `show` is the PR object a `forgectl show` returned after the snapshot was `too_large`.

    Only a terminal state is acted on: for `open` (or anything unreadable) the result is None and
    the caller counts the `too_large` failure. For `merged` and `closed` the plan carries the one
    `merged` or `closed` event (nothing else is diffed: the diff runs those two rules alone), the
    digest with only `state`, `merged_head_sha` and `merge_commit_sha` taken from `show` (every
    other part is the stored one; a row without a digest gets the same minimal digest with every
    `baselined` flag false and no `head_sha`), the terminal columns and `failure_count` 0. The
    incomplete counters, `baselined_at` and the settle columns' inputs are not touched. A stored
    `merged` row is final, as in `plan_snapshot`.

    The digest is not "only three fields": it also carries `baselined`, which is all false on a
    rebaseline row (deliberate: the row is baselined again silently).

    The event's `head_sha` is the stored digest's head when that is a valid sha (so no `pushed`
    can come from here), else the head `show` reports. With no valid head at all the `merged` or
    `closed` event is dropped with `events_dropped` (`invalid_head_sha`) and the row still goes
    terminal. A `show` of another PR raises `PrIdMismatch`, a non-mapping
    `MalformedSnapshot`.
    """
    if not isinstance(show, Mapping):
        raise MalformedSnapshot(type(show).__name__)
    if show.get("pr_id") != row.pr_id:
        raise PrIdMismatch(row.pr_id)
    state = show.get("state")
    if state not in ("merged", "closed"):
        return None
    stored = digest_from_json(row.digest)
    stamp = format_ts(now)
    if stored is not None and stored.get("state") == "merged":
        fields = {"digest": row.digest, "rebaseline": 0, "failure_count": 0,
                  "last_polled_at": stamp}  # fmt: skip
        return SnapshotPlan(DiffResult(), stored, fields, _counters(row.incomplete), ())
    view = {
        "state": state,
        "merged_head_sha": show.get("merged_head_sha"),
        "merge_commit_sha": show.get("merge_commit_sha"),
        "incomplete": list(PARTS),  # nothing else is read: every part keeps its stored value
        "head": {},
    }
    read = build_digest(view, stored, rebaseline=row.rebaseline)
    digest = (
        read
        if stored is None
        else {
            **stored,
            "state": read["state"],
            "merged_head_sha": read["merged_head_sha"],
            "merge_commit_sha": read["merge_commit_sha"],
            "baselined": read["baselined"],
        }
    )
    head = show.get("head")
    shown_head = head.get("sha") if isinstance(head, Mapping) else None
    stored_head = digest.get("head_sha")
    use_stored = isinstance(stored_head, str) and SHA_RE.match(stored_head)
    seen = {**digest, "head_sha": stored_head if use_stored else shown_head}
    events = diff(
        stored, seen, show, rebaseline=row.rebaseline, only=frozenset({"merged", "closed"})
    )
    same = row.status == "terminal" and row.terminal_kind == state and row.terminal_at
    fields: dict[str, Any] = {
        "digest": digest_to_json(digest),
        "rebaseline": 0,
        "failure_count": 0,
        "last_polled_at": stamp,
        "status": "terminal",
        "terminal_kind": state,
        "terminal_at": row.terminal_at if same else stamp,
    }
    refs = _refreshed(row, show)
    if refs is not None:
        fields["pr"], fields["repo"] = refs
    return SnapshotPlan(events, digest, fields, _counters(row.incomplete), ())
