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
  by `diff` (only `pushed`, `merged` and `closed` fire).
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

Left to other issues: `snap_updated_at` and `unsettled_since` (#22), and the `watch add` baseline
and `--no-baseline` (#18, which calls this with a row that has no digest).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from .diff import DiffResult, ForcedCheck, diff
from .digest import PARTS, build_digest, digest_from_json, digest_to_json, incomplete_parts
from .store import PR_RE, REPO_RE, format_ts

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
                  settle_polls=settle_polls)  # fmt: skip
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
