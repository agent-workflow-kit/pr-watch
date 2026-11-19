"""The compare-and-set commit of one PR's snapshot (docs/interface.md, Locks and commit).

`commit_snapshot` runs in one `BEGIN IMMEDIATE` transaction (`Store.transaction()`):

1. The PR row must still exist and still have a `sources` row, else the PR was removed while the
   snapshot was in flight (`SOURCE_REMOVED`); a row removed with its last source is never
   resurrected.
2. The snapshot's `pr_id` must equal the row's (`PR_ID_MISMATCH`; the caller runs
   the `pr_id_changed` failure path, no conflict is counted).
3. The row's `rev` must equal the revision the snapshot was based on (`REVISION`); the update
   statement repeats the check (`WHERE pr_id = ? AND rev = ?`) and bumps `rev`.
4. A snapshot that shows `closed` is discarded when a listing already saw a newer state
   (`listed_updated_at` larger than the snapshot's own `updated_at`, passed as
   `snapshot_updated_at`; a time that cannot be parsed counts as newer): `NEWER_LISTING`, no
   conflict counter, the persisted trigger retries it.

A discarded commit writes nothing, never calls `prepare` and reports why. Otherwise `prepare(conn)`
runs inside the transaction (the caller applies the membership rule and reads each event's labels
and watches there) and returns the events to store; they are appended with `append_event`, then the
fields are written, so the events, the digest and the revision move together. When `prepare` removed
the PR's last source the row is gone (trigger): the events and the deletion still commit and
`new_rev` is `None`. Any exception rolls everything back, and so does a kill: SQLite never publishes
a partial transaction.

The caller counts `prs.skipped_conflict` when `result.counts_as_conflict`. The lease is not checked
here on purpose: a taken-over holder may still commit, and this check decides.
"""

from __future__ import annotations

import enum
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from .store import PR_RE, REPO_RE, NewEvent, Store, StoredEvent, classify_error

# Columns of `prs` the snapshot-commit transaction writes (interface, State, transaction 2).
# `pr` and `repo` are refreshed from every snapshot (a rename or move keeps the `pr_id`). Identity
# (`pr_id`, `host`), `rev` and the columns owned by enumeration and the
# failure path (`listed_updated_at`, `last_attempt_at`, `last_failure_code`) are never writable.
WRITABLE_FIELDS: Final = frozenset(
    {
        "pr",
        "repo",
        "status",
        "terminal_at",
        "terminal_kind",
        "digest",
        "snap_updated_at",
        "last_polled_at",
        "failure_count",
        "incomplete",
        "unsettled_since",
        "rebaseline",
        "baselined_at",
    }
)


class DiscardReason(enum.Enum):
    REVISION = "revision"
    SOURCE_REMOVED = "source_removed"
    PR_ID_MISMATCH = "pr_id_mismatch"
    NEWER_LISTING = "newer_listing"


@dataclass(frozen=True)
class CommitResult:
    committed: bool
    reason: DiscardReason | None = None
    new_rev: int | None = None
    events: tuple[StoredEvent, ...] = ()

    @property
    def counts_as_conflict(self) -> bool:
        """Whether the caller counts this discard as `prs.skipped_conflict` (every discard but a
        closed snapshot overtaken by a listing; a `PR_ID_MISMATCH` is the caller's `pr_id_changed`
        failure path, not a conflict)."""
        return not self.committed and self.reason not in (
            DiscardReason.NEWER_LISTING,
            DiscardReason.PR_ID_MISMATCH,
        )


def _is_newer(listed: object, snapshot: object) -> bool:
    try:
        listed_at = datetime.fromisoformat(str(listed))
        snap_at = datetime.fromisoformat(str(snapshot))
    except ValueError:
        return True
    if listed_at.tzinfo is None or snap_at.tzinfo is None:
        return True
    return listed_at > snap_at


def _check(
    conn: sqlite3.Connection,
    pr_id: str,
    expected_rev: int,
    snapshot_pr_id: str,
    fields: Mapping[str, Any],
    snapshot_updated_at: str | None,
) -> DiscardReason | None:
    row = conn.execute(
        "SELECT rev, listed_updated_at, "
        "EXISTS (SELECT 1 FROM sources WHERE sources.pr_id = prs.pr_id) FROM prs WHERE pr_id = ?",
        (pr_id,),
    ).fetchone()
    if row is None or not row[2]:
        return DiscardReason.SOURCE_REMOVED
    if snapshot_pr_id != pr_id:
        return DiscardReason.PR_ID_MISMATCH
    if row[0] != expected_rev:
        return DiscardReason.REVISION
    if (
        fields.get("terminal_kind") == "closed"
        and row[1] is not None
        and _is_newer(row[1], snapshot_updated_at)
    ):
        return DiscardReason.NEWER_LISTING
    return None


_REF_FIELDS: Final = {"pr": PR_RE, "repo": REPO_RE}


def commit_snapshot(
    store: Store,
    *,
    pr_id: str,
    expected_rev: int,
    snapshot_pr_id: str,
    fields: Mapping[str, Any],
    prepare: Callable[[sqlite3.Connection], Sequence[NewEvent]] | None = None,
    snapshot_updated_at: str | None = None,
) -> CommitResult:
    """Commit one snapshot's fields and events if its source and revision still hold.

    `snapshot_updated_at` is the snapshot's own `updated_at` (not `snap_updated_at`, which may be
    the larger of it and the listed time); it is required when `fields` shows `closed`.
    """
    for name, value in fields.items():
        if name not in WRITABLE_FIELDS:
            raise ValueError(f"{name!r} is not a column a snapshot commit may write")
        if value is not None and not isinstance(value, int | float | str | bytes):
            raise TypeError(f"field {name!r} must be None, int, float, str or bytes")
        if name in _REF_FIELDS:
            ok = isinstance(value, str) and len(value) <= 256 and _REF_FIELDS[name].match(value)
            if not ok:
                raise ValueError(f"field {name!r} is not a valid reference")
    if fields.get("terminal_kind") == "closed" and snapshot_updated_at is None:
        raise ValueError("a closed snapshot needs snapshot_updated_at")
    discard: DiscardReason | None = None
    removed = False
    events: list[StoredEvent] = []
    with store.transaction() as conn:
        discard = _check(conn, pr_id, expected_rev, snapshot_pr_id, fields, snapshot_updated_at)
        if discard is None:
            for event in prepare(conn) if prepare is not None else ():
                events.append(store.append_event(event))
            if conn.execute("SELECT 1 FROM prs WHERE pr_id = ?", (pr_id,)).fetchone() is None:
                removed = True  # prepare dropped the last source (trigger): no row to update
            else:
                assignments = "".join(f"{name} = ?, " for name in fields)
                try:
                    cur = conn.execute(
                        f"UPDATE prs SET {assignments}rev = rev + 1 WHERE pr_id = ? AND rev = ?",  # noqa: S608  (names are allow-listed)
                        (*fields.values(), pr_id, expected_rev),
                    )
                except sqlite3.Error as exc:
                    raise classify_error(exc) from None
                if cur.rowcount != 1:  # unreachable under BEGIN IMMEDIATE; never commit on doubt
                    raise RuntimeError("compare-and-set update matched no row")
    if discard is not None:
        return CommitResult(committed=False, reason=discard)
    return CommitResult(
        committed=True, new_rev=None if removed else expected_rev + 1, events=tuple(events)
    )
