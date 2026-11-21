"""`events` and `export`: the stored events, offline and read-only (docs/interface.md, `events`).

Both commands read through the shared read-only open (`mode=ro`, `query_only`), never create
`state.db` (a missing store is an empty one) and read the head, the retention mark and the rows in
one read transaction, so a poll that commits meanwhile is either wholly in the result or wholly
after its cursor.

Judgment calls where the interface is silent:

- An empty store (no state directory, no `state.db`, a 0-byte file) has no `store_id`. It reports
  the placeholder id `aaaaaaaa` and head 0, and the normal rules apply (a cursor of any other id is
  a `gap`). A consumer that took the placeholder cursor before the first `watch add` sees one
  `gap` afterwards and re-syncs, which is the safe direction; the `cursor` of a `watch add` result
  avoids it.
- A cursor with a different `store_id` or a seq above the head is a gap and reads from the start
  of the store (nothing was ever delivered from it); a cursor below `pruned_through_seq` reads
  after its own seq (the oldest retained events follow it).
- `--pr` with a reference that no stored row names is looked up in the `pr` and `pr_id` columns of
  the events; the lookup is itself ambiguity-checked (two `pr_id`s behind one name is exit 2).
- `export --since D` selects `detected_at >= now - D`; `--since 0` therefore selects nothing.
  `export` has no limit: it prints every retained event that matches.
- The human text of `events` is one line per event and renders every stored string through
  `bounded_one_line`; `export` prints the JSON of each event on one line (JSON escapes stored
  control characters), whatever the global flags, and `--quiet` does not silence it (it is the
  command's data, not a rendering).
- No command here exposes `rev`, so the rename question of #25 needs no change: a rename (which
  poll writes with a `rev` bump) is invisible to `events`, `show` and `export`.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from .cli import CliError, Context, OutputError, ref_error_to_cli, store_error_to_cli
from .refs import PrId, PrRef, RefError, duration_ms, match_stored
from .store import DB_NAME, StoredEvent, classify_error, format_ts
from .text import bounded_one_line
from .watches import label_text, resolve_stored_pr

PLACEHOLDER_STORE_ID: Final = "aaaaaaaa"
DEFAULT_LIMIT: Final = 100

_COLUMNS: Final = "seq, id, type, pr_id, pr, payload, detected_at, labels"


def _now() -> datetime:
    return datetime.now(UTC)


@contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """One read transaction: every read inside sees the same database state."""
    conn.execute("BEGIN")
    try:
        yield
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")


def stored_event(row: Any) -> StoredEvent:
    """One `events` row. A payload that is not a JSON object, or labels that are not a JSON list of
    strings, is a corrupt store: a fixed one-line `internal` error naming the seq (no content)."""
    seq = int(row[0])
    try:
        payload = json.loads(row[5])
        labels = json.loads(row[7])
        if (
            not isinstance(payload, dict)
            or not isinstance(labels, list)
            or not all(isinstance(x, str) for x in labels)
        ):
            raise ValueError("malformed event")
    except (ValueError, TypeError):
        raise CliError("internal", f"{DB_NAME} holds a malformed event (seq {seq})") from None
    return StoredEvent(
        seq=seq,
        id=str(row[1]),
        type=str(row[2]),
        pr_id=str(row[3]),
        pr=str(row[4]),
        payload=payload,
        detected_at=str(row[6]),
        labels=labels,
    )


# --- filters -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Filters:
    pr_ids: list[str] | None
    types: list[str]
    labels: list[str]

    def sql(self) -> tuple[str, list[Any]]:
        """`" AND ..."` clauses over the `events` table and their parameters."""
        out = ""
        params: list[Any] = []
        if self.pr_ids is not None:
            out += f" AND pr_id IN ({', '.join('?' * len(self.pr_ids))})"
            params += self.pr_ids
        if self.types:
            out += f" AND type IN ({', '.join('?' * len(self.types))})"
            params += self.types
        for label in self.labels:
            # labels that are not a JSON array of strings (invalid JSON, a non-array, or an array
            # holding a non-string) pass the filter so that stored_event reports the row, not SQLite
            out += (
                " AND CASE"
                " WHEN NOT json_valid(events.labels) THEN 1"
                " WHEN json_type(events.labels) != 'array' THEN 1"
                " WHEN EXISTS (SELECT 1 FROM json_each(events.labels) WHERE type != 'text') THEN 1"
                " ELSE EXISTS (SELECT 1 FROM json_each(events.labels) WHERE value = ?)"
                " END"
            )
            params.append(label)
        return out, params


def _resolve_pr(conn: sqlite3.Connection | None, ref: PrRef | PrId) -> str | None:
    if conn is None:
        return None
    found = resolve_stored_pr(conn, ref)
    if found is not None:
        return found[0]
    if isinstance(ref, PrId):
        row = conn.execute("SELECT 1 FROM events WHERE pr_id = ? LIMIT 1", (ref.value,)).fetchone()
        return ref.value if row else None
    pairs = [
        (str(pr), str(pr_id))
        for pr, pr_id in conn.execute(
            "SELECT DISTINCT pr, pr_id FROM events WHERE lower(pr) = ?", (ref.key,)
        )
    ]
    return match_stored(ref, pairs)


def resolve_filters(conn: sqlite3.Connection | None, args: argparse.Namespace) -> Filters:
    pr_ids: list[str] | None = None
    if args.pr:
        try:
            found = [_resolve_pr(conn, ref) for ref in args.pr]
        except RefError as exc:
            raise ref_error_to_cli(exc) from None
        pr_ids = sorted({p for p in found if p is not None})
        if not pr_ids:
            raise CliError("not_found", "no stored PR or event matches the reference")
    return Filters(
        pr_ids=pr_ids,
        types=list(args.type or []),
        labels=[label_text(label) for label in (args.label or [])],
    )


def _read_rows(
    conn: sqlite3.Connection,
    after: int,
    filters: Filters,
    *,
    limit: int | None = None,
    detected_since: str | None = None,
) -> list[StoredEvent]:
    clause, params = filters.sql()
    if detected_since is not None:
        clause += " AND detected_at >= ?"
        params.append(detected_since)
    query = f"SELECT {_COLUMNS} FROM events WHERE seq > ?{clause} ORDER BY seq"  # noqa: S608
    args: list[Any] = [after, *params]
    if limit is not None:
        query += " LIMIT ?"
        args.append(limit)
    return [stored_event(r) for r in conn.execute(query, args)]


# --- events --------------------------------------------------------------------------------------


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None or row[0] is None else str(row[0])


def _int_meta(conn: sqlite3.Connection, key: str) -> int:
    try:
        return int(_meta(conn, key) or 0)
    except ValueError:
        return 0


def build(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    store = ctx.store
    limit: int = DEFAULT_LIMIT if args.limit is None else args.limit
    since: str | None = args.since
    if store is None:
        filters = resolve_filters(None, args)  # an unresolvable --pr is exit 10
        gap = since is not None and (
            since.split(".")[1] != PLACEHOLDER_STORE_ID or int(since.split(".")[2]) > 0
        )
        return {
            "events": [],
            "cursor": f"v1.{PLACEHOLDER_STORE_ID}.0",
            "gap": gap,
            "more": False,
            "last_poll_finished_at": None,
        }
    conn = store.conn
    try:
        with read_snapshot(conn):
            head = store.head_seq()  # first read: the snapshot starts here
            pruned = _int_meta(conn, "pruned_through_seq")
            polled = _meta(conn, "last_poll_finished_at")
            filters = resolve_filters(conn, args)
            gap = False
            rows: list[StoredEvent] = []
            if since is not None:
                _, store_id, seq_text = since.split(".")
                seq = int(seq_text)
                gap = store_id != store.store_id or seq > head or seq < pruned
                after = 0 if store_id != store.store_id or seq > head else seq
                rows = _read_rows(conn, after, filters, limit=limit + 1)
    except sqlite3.Error as exc:
        raise store_error_to_cli(classify_error(exc)) from None
    more = len(rows) > limit
    rows = rows[:limit]
    seq_out = rows[-1].seq if more else head
    return {
        "events": [e.to_json() for e in rows],
        "cursor": f"v1.{store.store_id}.{seq_out}",
        "gap": gap,
        "more": more,
        "last_poll_finished_at": polled or None,
    }


def _s(value: object) -> str:
    return "-" if value is None else bounded_one_line(str(value))


def _login(value: object) -> str:
    return _s(value.get("login")) if isinstance(value, dict) else "-"


def summary(e: dict[str, Any]) -> str:
    kind = e.get("type")
    if kind == "pushed":
        return f"{_s(e.get('from_sha'))[:12]}..{_s(e.get('to_sha'))[:12]}"
    if kind in ("comment", "review_comment"):
        return f"by {_login(e.get('author'))}"
    if kind == "review":
        return f"by {_login(e.get('author'))} {_s(e.get('review_state'))}"
    if kind in ("approved", "changes_requested", "approval_withdrawn"):
        return f"by {_login(e.get('reviewer'))}"
    if kind == "ci":
        return f"{_s(e.get('from'))} -> {_s(e.get('to'))}"
    return ""


def event_line(e: dict[str, Any]) -> str:
    labels = ",".join(_s(x) for x in e.get("labels", []))
    parts = [
        _s(e.get("seq")),
        _s(e.get("detected_at")),
        _s(e.get("type")),
        _s(e.get("pr")),
        summary(e),
        f"labels={labels or '-'}",
    ]
    return "  " + "  ".join(p for p in parts if p)


def render(d: dict[str, Any]) -> str:
    lines = [f"events ({len(d['events'])})"]
    lines += [event_line(e) for e in d["events"]]
    lines.append(
        f"cursor: {d['cursor']}  gap: {'yes' if d['gap'] else 'no'}"
        f"  more: {'yes' if d['more'] else 'no'}"
    )
    last = d["last_poll_finished_at"]
    lines.append(f"last poll finished: {'never' if last is None else _s(last)}")
    return "\n".join(lines)


def events_command(ctx: Context, args: argparse.Namespace) -> int:
    data = build(ctx, args)
    ctx.emit(data, human=render(data))
    return 0


# --- export --------------------------------------------------------------------------------------


def export_command(ctx: Context, args: argparse.Namespace) -> int:
    store = ctx.store
    if store is None:
        resolve_filters(None, args)
        return 0
    cutoff: str | None = None
    if args.since is not None:
        cutoff = format_ts(_now() - timedelta(milliseconds=duration_ms(args.since)))
    try:
        with read_snapshot(store.conn):
            filters = resolve_filters(store.conn, args)
            rows = _read_rows(store.conn, 0, filters, detected_since=cutoff)
    except sqlite3.Error as exc:
        raise store_error_to_cli(classify_error(exc)) from None
    if rows:
        if sys.stdout is None:
            raise OutputError
        try:
            sys.stdout.write("".join(json.dumps(e.to_json()) + "\n" for e in rows))
            sys.stdout.flush()
        except OSError as exc:
            raise OutputError from exc
    return 0
