"""Watches, owners, labels and members on the store tables (docs/interface.md: Watches, owners
and members; State).

Every function takes the open connection of a `Store.transaction()`: the caller owns the
transaction, so a command's reads and writes are one unit and a concurrent poll commit meets the
compare-and-set of `commit.py` (a PR row removed with its last source is never resurrected).

- A per-PR watch is keyed by `pr_id`; a query watch by `refs.watch_key`. A watch has one row per
  key (the `key` column is UNIQUE).
- Owners are reference-counted by rows of `owners`; a label is a row of `labels` owned by the
  (watch, owner) pair that added it and is removed with that owner's row (foreign key cascade).
  One owner never touches another owner's rows.
- The last owner leaving deletes the watch; its sources cascade and the `sources` trigger drops
  every PR row left without a source, digest included, in the same transaction.
- Adding is idempotent: every `add_*` returns whether anything was created.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from .refs import Label, PrId, PrRef, duration_ms, match_stored
from .store import format_ts

_ALPHABET: Final = "abcdefghijklmnopqrstuvwxyz234567"

# Watch kinds (the `kind` column).
PR_KIND: Final = "pr"
QUERY_KIND: Final = "query"

# The most labels a watch carries: the union over its owners (docs/interface.md, Labels). Keeps a
# watch's labels, copied onto every event it detects, well inside the store's per-event cap.
# `label add` enforces the cap via `label_count_after`; any other path that seeds labels (such as
# `watch add --label`, #18) must call it too and refuse with `too_many_labels`.
MAX_LABELS_PER_WATCH: Final = 64


def new_watch_id() -> str:
    """`w-` plus 10 lowercase base32 characters, drawn once when a watch is created."""
    return "w-" + "".join(secrets.choice(_ALPHABET) for _ in range(10))


def label_text(label: Label) -> str:
    return f"{label.key}={label.value}"


# --- watches ---------------------------------------------------------------------------------


def find_watch(conn: sqlite3.Connection, kind: str, key: str) -> str | None:
    """The id of the watch of `kind` with `key`, or None."""
    row = conn.execute("SELECT id FROM watches WHERE kind = ? AND key = ?", (kind, key)).fetchone()
    return None if row is None else str(row[0])


def watch_exists(conn: sqlite3.Connection, watch_id: str) -> bool:
    return conn.execute("SELECT 1 FROM watches WHERE id = ?", (watch_id,)).fetchone() is not None


def ensure_watch(conn: sqlite3.Connection, kind: str, key: str, now: datetime) -> tuple[str, bool]:
    """The id of the watch with `key`, creating it when absent: `(id, created)`."""
    found = find_watch(conn, kind, key)
    if found is not None:
        return found, False
    watch_id = new_watch_id()
    conn.execute(
        "INSERT INTO watches (id, kind, key, created_at) VALUES (?, ?, ?, ?)",
        (watch_id, kind, key, format_ts(now)),
    )
    return watch_id, True


def pr_watch_id(conn: sqlite3.Connection, pr_id: str) -> str | None:
    """The per-PR watch of a stored PR, or None."""
    return find_watch(conn, PR_KIND, pr_id)


def resolve_stored_pr(conn: sqlite3.Connection, ref: PrRef | PrId) -> tuple[str, str] | None:
    """`(pr_id, pr)` of the stored row a reference names, or None. Raises RefError `usage`
    (`ambiguous_reference`) when a PR name matches more than one stored row. Offline."""
    if isinstance(ref, PrId):
        rows = conn.execute("SELECT pr, pr_id FROM prs WHERE pr_id = ?", (ref.value,)).fetchall()
    else:
        # lower() folds ASCII only, like refs._ascii_lower.
        rows = conn.execute("SELECT pr, pr_id FROM prs WHERE lower(pr) = ?", (ref.key,)).fetchall()
    pairs = [(str(pr), str(pr_id)) for pr, pr_id in rows]
    pr_id = match_stored(ref, pairs)
    if pr_id is None:
        return None
    return pr_id, next(pr for pr, found in pairs if found == pr_id)


# --- owners and labels -------------------------------------------------------------------------


def holds(conn: sqlite3.Connection, watch_id: str, owner: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM owners WHERE watch_id = ? AND owner = ?", (watch_id, owner)
        ).fetchone()
        is not None
    )


def add_owner(conn: sqlite3.Connection, watch_id: str, owner: str) -> bool:
    """Add the owner's reference; False when it already held one."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO owners (watch_id, owner) VALUES (?, ?)", (watch_id, owner)
    )
    return cur.rowcount > 0


def label_count_after(conn: sqlite3.Connection, watch_id: str, labels: Iterable[Label]) -> int:
    """How many distinct labels the watch would carry after `labels` are added (any owner's label
    already present does not count twice)."""
    union = set(labels_of(conn, watch_id))
    union.update(label_text(label) for label in labels)
    return len(union)


def add_labels(
    conn: sqlite3.Connection, watch_id: str, owner: str, labels: Iterable[Label]
) -> bool:
    """Add labels to the (watch, owner) pair, which must hold a reference (the foreign key
    refuses anything else). True when at least one label was new."""
    changed = False
    for label in labels:
        cur = conn.execute(
            "INSERT OR IGNORE INTO labels (watch_id, owner, label) VALUES (?, ?, ?)",
            (watch_id, owner, label_text(label)),
        )
        changed = changed or cur.rowcount > 0
    return changed


def remove_labels(
    conn: sqlite3.Connection, watch_id: str, owner: str, labels: Iterable[Label]
) -> bool:
    """Remove the owner's own labels only; another owner's identical label stays. True when at
    least one label was present."""
    changed = False
    for label in labels:
        cur = conn.execute(
            "DELETE FROM labels WHERE watch_id = ? AND owner = ? AND label = ?",
            (watch_id, owner, label_text(label)),
        )
        changed = changed or cur.rowcount > 0
    return changed


@dataclass(frozen=True)
class Removal:
    """The outcome of `remove_owner`."""

    held: bool  # the owner held a reference (False: nothing changed)
    watch_ended: bool
    prs_dropped: int


def remove_owner(conn: sqlite3.Connection, watch_id: str, owner: str) -> Removal:
    """Remove only this owner's reference and labels. When no owner is left the watch ends: its
    sources go with it and every PR row left without a source is dropped (trigger)."""
    cur = conn.execute("DELETE FROM owners WHERE watch_id = ? AND owner = ?", (watch_id, owner))
    if cur.rowcount == 0:
        return Removal(held=False, watch_ended=False, prs_dropped=0)
    if conn.execute("SELECT 1 FROM owners WHERE watch_id = ?", (watch_id,)).fetchone():
        return Removal(held=True, watch_ended=False, prs_dropped=0)
    had = [
        str(row[0])
        for row in conn.execute("SELECT pr_id FROM sources WHERE watch_id = ?", (watch_id,))
    ]
    conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
    dropped = sum(
        1
        for pr_id in had
        if conn.execute("SELECT 1 FROM prs WHERE pr_id = ?", (pr_id,)).fetchone() is None
    )
    return Removal(held=True, watch_ended=True, prs_dropped=dropped)


# --- sources and members -------------------------------------------------------------------------


def add_source(conn: sqlite3.Connection, pr_id: str, watch_id: str) -> bool:
    """Record that the watch includes the PR (the row must exist). When a PR moves between
    watches, add the new source before removing the old one: the last source's removal drops the
    row and its digest."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO sources (pr_id, watch_id) VALUES (?, ?)", (pr_id, watch_id)
    )
    return cur.rowcount > 0


# --- expiry ------------------------------------------------------------------------------------

# The expiry predicate (Watches: PR status): a `terminal` row whose `terminal_at` is older than
# the cutoff. Timestamps are fixed-width UTC, so text order is time order. `prune` and `watch
# add` evaluate it inside the transaction that deletes the row. An `unreachable` row never
# expires by itself.
EXPIRED_PREDICATE: Final = "status = 'terminal' AND terminal_at IS NOT NULL AND terminal_at < ?"


def expiry_cutoff(now: datetime, after_terminal: str) -> str:
    """The `terminal_at` bound for `EXPIRED_PREDICATE`: `now` minus `expiry.after_terminal`."""
    return format_ts(now - timedelta(milliseconds=duration_ms(after_terminal)))


def is_expired(terminal_at: str | None, cutoff: str) -> bool:
    return terminal_at is not None and terminal_at < cutoff


# --- the watch object (schemas/defs.json `watch`) ------------------------------------------------


def labels_of(conn: sqlite3.Connection, watch_id: str, owner: str | None = None) -> list[str]:
    """The labels of a watch: the union over its owners, or one owner's own. Sorted."""
    if owner is None:
        rows = conn.execute("SELECT DISTINCT label FROM labels WHERE watch_id = ?", (watch_id,))
    else:
        rows = conn.execute(
            "SELECT label FROM labels WHERE watch_id = ? AND owner = ?", (watch_id, owner)
        )
    return sorted(str(row[0]) for row in rows)


def _query_hosts(key: str) -> list[str]:
    hosts: list[str] = []
    repos: list[str] = []
    for part in key.split(";")[1:]:
        name, _, value = part.partition("=")
        (hosts if name == "host" else repos).extend(value.split(","))
    return sorted({*hosts, *(repo.partition("/")[0] for repo in repos)}) if hosts or repos else []


def _enumerated_at(conn: sqlite3.Connection, key: str, created_at: str) -> str | None:
    """The oldest `hosts.last_enumerated_at` over the hosts the query lists; None while any is
    null or older than the watch, or when no host is known (an unfiltered query lists every host
    in scope, which is known only after the first poll)."""
    hosts = _query_hosts(key)
    if not hosts:
        return None
    stamps: list[str] = []
    for host in hosts:
        row = conn.execute(
            "SELECT last_enumerated_at FROM hosts WHERE host = ?", (host,)
        ).fetchone()
        if row is None or row[0] is None or str(row[0]) < created_at:
            return None
        stamps.append(str(row[0]))
    return min(stamps)


def watch_view(conn: sqlite3.Connection, watch_id: str) -> dict[str, Any] | None:
    """The `watch` object of schemas/defs.json as stored now, or None when the watch is gone."""
    row = conn.execute(
        "SELECT kind, key, created_at FROM watches WHERE id = ?", (watch_id,)
    ).fetchone()
    if row is None:
        return None
    kind, key, created_at = str(row[0]), str(row[1]), str(row[2])
    owners = sorted(
        str(r[0]) for r in conn.execute("SELECT owner FROM owners WHERE watch_id = ?", (watch_id,))
    )
    view: dict[str, Any] = {
        "id": watch_id,
        "kind": kind,
        "key": key,
        "pr": None,
        "owners": owners,
        "labels": labels_of(conn, watch_id),
        "members": None,
        "created_at": created_at,
        "enumerated_at": None,
        "pending_baseline": None,
    }
    if kind == PR_KIND:
        pr = conn.execute("SELECT pr FROM prs WHERE pr_id = ?", (key,)).fetchone()
        view["pr"] = None if pr is None else str(pr[0])
    else:
        counts = conn.execute(
            "SELECT count(*), coalesce(sum(prs.digest IS NULL), 0) FROM sources "
            "JOIN prs ON prs.pr_id = sources.pr_id WHERE sources.watch_id = ?",
            (watch_id,),
        ).fetchone()
        view["members"] = int(counts[0])
        view["pending_baseline"] = int(counts[1])
        view["enumerated_at"] = _enumerated_at(conn, key, created_at)
    return view


# --- event context and expiry ---------------------------------------------------------------------


def event_context(conn: sqlite3.Connection, pr_id: str) -> tuple[list[str], list[str]]:
    """`(labels, watch ids)` for an event on the PR: the label union over, and the ids of, every
    watch that includes it now (read inside the commit transaction). Both sorted."""
    ids = sorted(
        str(r[0]) for r in conn.execute("SELECT watch_id FROM sources WHERE pr_id = ?", (pr_id,))
    )
    labels = sorted(
        {
            str(r[0])
            for r in conn.execute(
                "SELECT label FROM labels WHERE watch_id IN "
                "(SELECT watch_id FROM sources WHERE pr_id = ?)",
                (pr_id,),
            )
        }
    )
    return labels, ids


@dataclass(frozen=True)
class ExpiredDrop:
    """What `drop_expired_row` removed besides the PR row."""

    watches_ended: int  # the per-PR watch (0 or 1; owners and labels cascade)
    members_dropped: int  # query watches that lost the PR as a member


def expired_pr_ids(conn: sqlite3.Connection, cutoff: str) -> list[str]:
    """The `pr_id` of every row satisfying the expiry predicate now, in a stable order."""
    rows = conn.execute(
        f"SELECT pr_id FROM prs WHERE {EXPIRED_PREDICATE} ORDER BY pr_id",  # noqa: S608  (constant)
        (cutoff,),
    )
    return [str(row[0]) for row in rows]


def expiry_effects(conn: sqlite3.Connection, pr_id: str) -> ExpiredDrop:
    """What deleting the row would end: its per-PR watch and its query-watch memberships."""
    ended = conn.execute(
        "SELECT count(*) FROM watches WHERE kind = ? AND key = ?", (PR_KIND, pr_id)
    ).fetchone()
    members = conn.execute(
        "SELECT count(*) FROM sources JOIN watches ON watches.id = sources.watch_id "
        "WHERE sources.pr_id = ? AND watches.kind = ?",
        (pr_id, QUERY_KIND),
    ).fetchone()
    return ExpiredDrop(watches_ended=int(ended[0]), members_dropped=int(members[0]))


def drop_expired_row(conn: sqlite3.Connection, pr_id: str, cutoff: str) -> ExpiredDrop | None:
    """Delete the PR's row when it satisfies the expiry predicate, and end its per-PR watch (owners
    and labels go with it; a query watch just loses the member). None when the row does not
    satisfy the predicate now. The predicate is evaluated here, inside the caller's transaction:
    the one implementation `prune` and `watch add` share. It leaves `leases` and `scopes` rows
    (no foreign key); `prune` clears those."""
    hit = conn.execute(
        f"SELECT 1 FROM prs WHERE pr_id = ? AND {EXPIRED_PREDICATE}",  # noqa: S608  (constant)
        (pr_id, cutoff),
    ).fetchone()
    if hit is None:
        return None
    effects = expiry_effects(conn, pr_id)
    conn.execute("DELETE FROM watches WHERE kind = ? AND key = ?", (PR_KIND, pr_id))
    conn.execute("DELETE FROM prs WHERE pr_id = ?", (pr_id,))
    return effects


def drop_expired_pr(conn: sqlite3.Connection, pr_id: str, cutoff: str) -> bool:
    """`drop_expired_row` for `watch add`: True when the row was deleted."""
    return drop_expired_row(conn, pr_id, cutoff) is not None
