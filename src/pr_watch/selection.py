"""Poll cycle step 4: choose the snapshots (docs/interface.md: Poll cycle, State, Pacing).

`choose_snapshots` reads only persisted columns, never in-memory marks, so a crash, a deferral, a
pause or low water loses no trigger. Only `active` rows are chosen by (a) to (e) and only
`unreachable` rows by (f); a `terminal` row never. The groups, in priority order:

- (b) `listed_updated_at` greater than `snap_updated_at` (a null or unreadable `snap_updated_at`
  with a listing counts; an unreadable `listed_updated_at` does not, enumeration validates it);
- (d) a source with `left_count` 2;
- (a) no digest, or `rebaseline`;
- (c) unsettled and not `ci_stuck` (`unsettled_since` null counts as not stuck);
- (e) the host's full pass is in progress and the row is not done for it (`last_polled_at` or
  `last_attempt_at` at or after the pass start); (f) the same for an `unreachable` row.

A row is in the first group that applies. Inside a group the oldest `last_polled_at` goes first
(never polled first, then `pr_id`, so the order is deterministic). A host's `max_snapshots` (0:
unlimited) caps the list; the rest is `deferred` and keeps its triggers. While a pass has (e) or
(f) rows waiting, group (c) takes at most three quarters of the cap (rounded down). A host in
low water (`HostPlan.limited`) gets groups (a) to (d) only. Hosts with no `HostPlan` (paused,
failed, not listed) get nothing.

Memory: the table is streamed; only the chosen rows' keys are kept, and the digest is parsed
only for rows whose text can hold an unsettled marker.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from .digest import digest_from_json
from .enumeration import parse_stamp
from .settle import is_stuck, is_unsettled

GROUPS: Final = ("b", "d", "a", "c", "e", "f")
_RANK: Final = {g: i for i, g in enumerate(GROUPS)}
C_SHARE: Final = (3, 4)

# A digest can only be unsettled when `ci.state` is `pending`, `ci.hold` is above 0 or the text
# holds `mergeability_unknown` (the exact answer is `settle.is_unsettled`); the prefilter spares
# parsing every digest of a large store. Text that is not valid JSON is never unsettled.
_ROWS: Final = (
    "SELECT pr_id, pr, host, status, digest IS NULL, rebaseline, snap_updated_at, "
    "listed_updated_at, last_polled_at, last_attempt_at, unsettled_since, "
    "CASE WHEN json_valid(digest) AND (json_extract(digest, '$.ci.state') = 'pending' "
    "OR json_extract(digest, '$.ci.hold') > 0 "
    "OR instr(digest, 'mergeability_unknown') > 0) THEN digest END, "
    "EXISTS (SELECT 1 FROM sources s WHERE s.pr_id = prs.pr_id AND s.left_count >= 2) "
    "FROM prs WHERE status IN ('active', 'unreachable')"
)


@dataclass(frozen=True)
class HostPlan:
    """What the pacing stage (#23) decided for one host this cycle."""

    host: str
    concurrency: int
    max_snapshots: int = 0  # 0: unlimited (also `--full`)
    pass_started_at: datetime | None = None  # a full pass is in progress
    limited: bool = False  # low water: groups (a) to (d) only


@dataclass(frozen=True)
class Task:
    pr_id: str
    pr: str
    host: str
    group: str
    # the pool re-checks the trigger once it holds the lease (a concurrent poll may have
    # committed in between); a forced task (`--pr`, `--full`) is never skipped
    forced: bool = False
    pass_started_at: datetime | None = None  # set for groups (e) and (f)


@dataclass
class Selection:
    tasks: dict[str, list[Task]] = field(default_factory=dict)
    deferred: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(len(t) for t in self.tasks.values())

    @property
    def total_deferred(self) -> int:
        return sum(self.deferred.values())


def _changed(listed: object, snap: object) -> bool:
    listed_at = parse_stamp(listed)
    if listed_at is None:
        return False
    snap_at = parse_stamp(snap)
    return snap_at is None or listed_at > snap_at


def _done(started: datetime, *stamps: object) -> bool:
    return any((at := parse_stamp(s)) is not None and at >= started for s in stamps)


def _group(row: tuple[object, ...], plan: HostPlan, now: datetime, age_ms: int) -> str | None:
    (_, _, _, status, no_digest, rebaseline, snap, listed, polled, attempt, since, text, left) = (
        row[:13]
    )
    started = plan.pass_started_at if not plan.limited else None
    if status == "unreachable":
        return "f" if started is not None and not _done(started, polled, attempt) else None
    if _changed(listed, snap):
        return "b"
    if left:
        return "d"
    if no_digest or rebaseline:
        return "a"
    if text is not None and is_unsettled(digest_from_json(str(text))):
        if not is_stuck(None if since is None else str(since), now, age_ms):
            return "c"
    if started is not None and not _done(started, polled, attempt):
        return "e"
    return None


def _order(row: tuple[object, ...]) -> tuple[str, str]:
    at = parse_stamp(row[8])
    return ("" if at is None else at.strftime("%Y%m%d%H%M%S%f"), str(row[0]))


def choose_snapshots(
    conn: sqlite3.Connection,
    plans: Mapping[str, HostPlan],
    *,
    now: datetime,
    pending_max_age_ms: int,
    only_pr_ids: Collection[str] | None = None,
) -> Selection:
    """The snapshots of this cycle, per host, in the order they start. `only_pr_ids` restricts
    the candidates (a `--pr` or `--repo` poll)."""
    buckets: dict[str, dict[str, list[tuple[tuple[str, str], str, str]]]] = {}
    for row in conn.execute(_ROWS):
        plan = plans.get(str(row[2]))
        if plan is None or (only_pr_ids is not None and row[0] not in only_pr_ids):
            continue
        group = _group(tuple(row), plan, now, pending_max_age_ms)
        if group is not None:
            buckets.setdefault(plan.host, {}).setdefault(group, []).append(
                (_order(tuple(row)), str(row[0]), str(row[1]))
            )
    out = Selection()
    for host in sorted(buckets):
        plan = plans[host]
        by_group = buckets[host]
        for rows in by_group.values():
            rows.sort()
        waiting = any(by_group.get(g) for g in ("e", "f"))
        cap = plan.max_snapshots if plan.max_snapshots > 0 else None
        tasks: list[Task] = []
        eligible = sum(len(rows) for rows in by_group.values())
        for group in GROUPS:
            room = None if cap is None else cap - len(tasks)
            if group == "c" and cap is not None and waiting:
                room = min(room if room is not None else cap, cap * C_SHARE[0] // C_SHARE[1])
            rows = by_group.get(group, [])
            for _, pr_id, pr in rows if room is None else rows[: max(room, 0)]:
                started = plan.pass_started_at if group in ("e", "f") else None
                tasks.append(Task(pr_id, pr, host, group, pass_started_at=started))
        out.tasks[host] = tasks
        if eligible > len(tasks):
            out.deferred[host] = eligible - len(tasks)
    return out


def still_due(
    conn: sqlite3.Connection, task: Task, *, now: datetime, pending_max_age_ms: int
) -> bool:
    """Whether the task's trigger still holds on the stored row (read again once the pool holds
    the lease). A forced task always does; a row that is gone, `terminal` or in no group does
    not."""
    if task.forced:
        return True
    row = conn.execute(_ROWS + " AND pr_id = ?", (task.pr_id,)).fetchone()
    if row is None:
        return False
    plan = HostPlan(task.host, 1, pass_started_at=task.pass_started_at)
    return _group(tuple(row), plan, now, pending_max_age_ms) is not None
