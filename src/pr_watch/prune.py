"""`prune`: event retention, row expiry and lease cleanup (docs/interface.md, `prune`).

Two transactions kinds, so a concurrent `poll` commit is never starved for long:

1. One transaction: expired rows (the predicate `watches.EXPIRED_PREDICATE`, evaluated per row
   inside it by `watches.drop_expired_row`, the same code `watch add` uses), then the `leases` and
   `scopes` rows nothing refers to any more (the tables have no foreign key to `prs`).
2. Events older than the retention cutoff, in batches of `EVENT_BATCH`, one transaction per
   batch. Each batch raises `meta.pruned_through_seq` to the highest seq it deleted (never
   lowers it) in the same transaction as the delete. An event is deleted only when its own
   `detected_at` is older than the cutoff, so nothing newer is touched, and a clock stepped back
   (a cutoff in the past) deletes less, never more.

Finally `last_prune_at` is stamped and the WAL checkpointed (`TRUNCATE`). `should_stop` (the
poll's signal flag) is checked before each phase and between event batches: a stopped run returns
what it did so far and stamps nothing, so the next poll prunes again.

`prune --dry-run` is read-only end to end: the CLI opens the store `mode=ro` (or finds none) and
`plan` reads inside one `BEGIN ... ROLLBACK`; it computes the same counts the real run returns.

Judgment calls where the interface is silent:

- A lease is deleted when it is expired, its holder is dead, or its PR has no row (orphaned,
  including rows this run expires): a live holder's lease for a row that is gone has nothing to
  protect (its commit is discarded by the compare-and-set).
- A `scopes` row is deleted only when no `prs` row has its repo. `poll` owns the finer rule (no
  active or closed-inside-expiry per-PR row), so a scope that `poll` might still use is kept.
- `prune` writes no `log.jsonl` record: `schemas/log.json` defines none for it.
- The result counts `watches_ended` (per-PR watches deleted with an expired row) and
  `members_dropped` (query-watch memberships of expired rows); a query watch itself stays.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Final

from . import watches
from .cli import Context, store_error_to_cli
from .leases import MAX_LEASE_TTL, Leases
from .refs import duration_ms
from .store import Store, StoreError, classify_error, format_ts

EVENT_BATCH: Final = 1000


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class PruneResult:
    events_deleted: int = 0
    prs_deleted: int = 0
    watches_ended: int = 0
    members_dropped: int = 0
    leases_deleted: int = 0
    dry_run: bool = False

    def to_json(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "events_deleted": self.events_deleted,
            "prs_deleted": self.prs_deleted,
            "watches_ended": self.watches_ended,
            "members_dropped": self.members_dropped,
            "leases_deleted": self.leases_deleted,
        }


def _cutoffs(now: datetime, retention: str, after_terminal: str) -> tuple[str, str]:
    """`(events cutoff, expiry cutoff)`: events detected before the first and rows terminal before
    the second are due."""
    return (
        format_ts(now - timedelta(milliseconds=duration_ms(retention))),
        watches.expiry_cutoff(now, after_terminal),
    )


def _leases(store: Store, now: datetime) -> Leases:
    return Leases(store, MAX_LEASE_TTL, clock=lambda: now)


# --- the real run ----------------------------------------------------------------------------


def _orphan_scopes(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT repo FROM scopes WHERE NOT EXISTS (SELECT 1 FROM prs WHERE prs.repo = scopes.repo)"
    )
    return [str(r[0]) for r in rows]


def _expire_and_clean(store: Store, now: datetime, expiry_cutoff: str) -> PruneResult:
    leases = _leases(store, now)
    with store.transaction() as conn:
        dropped = 0
        ended = 0
        members = 0
        for pr_id in watches.expired_pr_ids(conn, expiry_cutoff):
            effects = watches.drop_expired_row(conn, pr_id, expiry_cutoff)
            if effects is not None:
                dropped += 1
                ended += effects.watches_ended
                members += effects.members_dropped
        doomed = set(leases.stale_pr_ids(conn))
        doomed.update(
            str(r[0])
            for r in conn.execute(
                "SELECT pr_id FROM leases WHERE NOT EXISTS "
                "(SELECT 1 FROM prs WHERE prs.pr_id = leases.pr_id)"
            )
        )
        for pr_id in doomed:
            conn.execute("DELETE FROM leases WHERE pr_id = ?", (pr_id,))
        for repo in _orphan_scopes(conn):
            conn.execute("DELETE FROM scopes WHERE repo = ?", (repo,))
    return PruneResult(
        prs_deleted=dropped,
        watches_ended=ended,
        members_dropped=members,
        leases_deleted=len(doomed),
    )


def _pruned_through(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'pruned_through_seq'").fetchone()
    try:
        return int(str(row[0])) if row else 0
    except ValueError:
        return 0


def _delete_event_batch(store: Store, cutoff: str) -> int:
    """Delete up to `EVENT_BATCH` events older than `cutoff`, oldest seq first, and raise
    `pruned_through_seq` in the same transaction. Returns how many were deleted."""
    with store.transaction() as conn:
        seqs = [
            int(r[0])
            for r in conn.execute(
                "SELECT seq FROM events WHERE detected_at < ? ORDER BY seq LIMIT ?",
                (cutoff, EVENT_BATCH),
            )
        ]
        if not seqs:
            return 0
        conn.executemany("DELETE FROM events WHERE seq = ?", [(seq,) for seq in seqs])
        top = max(_pruned_through(conn), max(seqs))  # monotonic: never goes back
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('pruned_through_seq', ?)",
            (str(top),),
        )
        return len(seqs)


def _checkpoint(store: Store) -> None:
    """`wal_checkpoint(TRUNCATE)` without waiting: a reader holding a snapshot would otherwise
    make it block writers for up to `busy_timeout`. A busy or failed checkpoint is not an error:
    the next prune or poll retries."""
    conn = store.conn
    previous = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
    conn.execute("PRAGMA busy_timeout = 0")
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    except sqlite3.Error:
        pass
    finally:
        conn.execute(f"PRAGMA busy_timeout = {previous}")


def prune(
    store: Store,
    now: datetime,
    retention: str,
    after_terminal: str,
    should_stop: Callable[[], bool] | None = None,
) -> PruneResult:
    """Apply retention, expiry and lease cleanup. Also the housekeeping `poll` runs once a day.

    With `should_stop` a true answer ends the run between phases and event batches without
    stamping `last_prune_at` (the result counts what was done)."""

    def stopped() -> bool:
        return should_stop is not None and should_stop()

    events_cutoff, expiry_cutoff = _cutoffs(now, retention, after_terminal)
    if stopped():
        return PruneResult()
    cleaned = _expire_and_clean(store, now, expiry_cutoff)
    deleted = 0
    while True:
        if stopped():
            return replace(cleaned, events_deleted=deleted)
        batch = _delete_event_batch(store, events_cutoff)
        deleted += batch
        if batch < EVENT_BATCH:
            break
    if stopped():
        return replace(cleaned, events_deleted=deleted)
    with store.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_prune_at', ?)",
            (format_ts(now),),
        )
    _checkpoint(store)
    return PruneResult(
        events_deleted=deleted,
        prs_deleted=cleaned.prs_deleted,
        watches_ended=cleaned.watches_ended,
        members_dropped=cleaned.members_dropped,
        leases_deleted=cleaned.leases_deleted,
    )


# --- the dry run -----------------------------------------------------------------------------


def plan(store: Store | None, now: datetime, retention: str, after_terminal: str) -> PruneResult:
    """What `prune` would delete, read in one `BEGIN ... ROLLBACK` snapshot; writes nothing. A
    missing store (None) is an empty one."""
    if store is None:
        return PruneResult(dry_run=True)
    events_cutoff, expiry_cutoff = _cutoffs(now, retention, after_terminal)
    conn = store.conn
    try:
        conn.execute("BEGIN")
        try:
            events = int(
                conn.execute(
                    "SELECT count(*) FROM events WHERE detected_at < ?", (events_cutoff,)
                ).fetchone()[0]
            )
            expired = watches.expired_pr_ids(conn, expiry_cutoff)
            effects = [watches.expiry_effects(conn, pr_id) for pr_id in expired]
            doomed = set(_leases(store, now).stale_pr_ids(conn))
            gone = set(expired)
            for (pr_id,) in conn.execute("SELECT pr_id FROM leases").fetchall():
                present = conn.execute("SELECT 1 FROM prs WHERE pr_id = ?", (pr_id,)).fetchone()
                if present is None or str(pr_id) in gone:
                    doomed.add(str(pr_id))
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
    except sqlite3.Error as exc:
        raise classify_error(exc) from None
    return PruneResult(
        events_deleted=events,
        prs_deleted=len(expired),
        watches_ended=sum(e.watches_ended for e in effects),
        members_dropped=sum(e.members_dropped for e in effects),
        leases_deleted=len(doomed),
        dry_run=True,
    )


# --- the command -----------------------------------------------------------------------------


def _plural(n: int, noun: str, plural: str | None = None) -> str:
    return f"{n} {noun if n == 1 else plural or noun + 's'}"


def render(result: PruneResult) -> str:
    verb = "would delete" if result.dry_run else "deleted"
    return (
        f"{verb}: {_plural(result.events_deleted, 'event')}, "
        f"{_plural(result.prs_deleted, 'PR')} "
        f"({_plural(result.watches_ended, 'watch', 'watches')} ended, "
        f"{_plural(result.members_dropped, 'member')} dropped), "
        f"{_plural(result.leases_deleted, 'lease')}"
    )


def prune_command(ctx: Context, args: argparse.Namespace) -> int:
    cfg = ctx.loaded.config
    now = _now()
    retention, after_terminal = cfg.retention.events, cfg.expiry.after_terminal
    try:
        if args.dry_run:
            result = plan(ctx.store, now, retention, after_terminal)
        else:
            if ctx.store is None:
                raise StoreError("the state store is not open")
            result = prune(ctx.store, now, retention, after_terminal)
    except StoreError as exc:
        raise store_error_to_cli(exc) from None
    ctx.emit(result.to_json(), human=render(result))
    return 0
