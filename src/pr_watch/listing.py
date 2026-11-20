"""`list`: the watches, the PRs and the host state of the store (docs/interface.md, `list`).

Offline and read-only: the handler never writes, never creates `state.db` (a missing store is an
empty one) and reads everything in one read transaction, so a concurrent poll commit cannot show
half of its effect.

Judgment calls where the interface is silent:

- `--label` is repeatable and ANDed. Without `--owner` a label counts when any owner added it
  (the union a watch carries); with `--owner` only that owner's own labels count (a label belongs
  to the (watch, owner) pair).
- `--owner` and `--label` select watches; the PRs shown are those with a source among them. A
  watch that is shown keeps all its owners and labels.
- `--pr` naming a PR that is not stored (or an ambiguous-free miss) selects nothing: exit 0 with
  an empty `prs`, unlike `show` (exit 10). It intersects with the watch filters and `--status`
  and never narrows `watches` or `hosts`.
- Watches are ordered by `created_at` then id, PRs by canonical `pr` then `pr_id`, hosts by name.
- `enumerated_at` of an unfiltered query watch stays null (as `watch_view` prints it).
- The human text renders every stored string through `bounded_one_line`, so a stored newline or
  control character can neither forge a line nor reach the terminal.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from . import watches
from .cli import Context, ref_error_to_cli, store_error_to_cli
from .refs import PrId, PrRef, RefError, duration_ms
from .rows import pr_row
from .store import classify_error
from .text import bounded_one_line
from .watches import label_text, labels_of, watch_view

_EMPTY: dict[str, Any] = {"watches": [], "prs": [], "last_poll_finished_at": None, "hosts": []}


@contextmanager
def _snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN")
    try:
        yield
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")


def _selected_watches(conn: sqlite3.Connection, args: argparse.Namespace) -> list[str]:
    wanted = {label_text(label) for label in (args.label or [])}
    owner: str | None = args.owner
    ids: list[str] = []
    rows = conn.execute("SELECT id FROM watches ORDER BY created_at, id").fetchall()
    for (watch_id,) in rows:
        wid = str(watch_id)
        if owner is not None and not watches.holds(conn, wid, owner):
            continue
        if wanted and not wanted <= set(labels_of(conn, wid, owner)):
            continue
        ids.append(wid)
    return ids


def _pr_ids(
    conn: sqlite3.Connection, args: argparse.Namespace, watch_ids: list[str] | None
) -> list[str]:
    """The pr_ids to print, in order. `watch_ids` is None when no watch filter is active."""
    ref: PrRef | PrId | None = args.pr
    only: str | None = None
    if ref is not None:
        try:
            found = watches.resolve_stored_pr(conn, ref)
        except RefError as exc:
            raise ref_error_to_cli(exc) from None
        if found is None:
            return []
        only = found[0]
    query = "SELECT pr_id FROM prs"
    where: list[str] = []
    params: list[Any] = []
    if args.status is not None:
        where.append("status = ?")
        params.append(args.status)
    if only is not None:
        where.append("pr_id = ?")
        params.append(only)
    if watch_ids is not None:
        marks = ", ".join("?" * len(watch_ids))
        where.append(f"pr_id IN (SELECT pr_id FROM sources WHERE watch_id IN ({marks}))")
        params.extend(watch_ids)
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY pr, pr_id"
    return [str(r[0]) for r in conn.execute(query, params)]


def _now() -> datetime:
    return datetime.now(UTC)


def build(ctx: Context, args: argparse.Namespace, *, now: datetime | None = None) -> dict[str, Any]:
    store = ctx.store
    if store is None:
        return {k: ([] if isinstance(v, list) else v) for k, v in _EMPTY.items()}
    conn = store.conn
    now = _now() if now is None else now
    pending_ms = duration_ms(ctx.loaded.config.ci.pending_max_age)
    filtered = args.owner is not None or bool(args.label)
    try:
        with _snapshot(conn):
            watch_ids = _selected_watches(conn, args)
            views = [v for w in watch_ids if (v := watch_view(conn, w)) is not None]
            prs = [
                row
                for pr_id in _pr_ids(conn, args, watch_ids if filtered else None)
                if (row := pr_row(conn, pr_id, now=now, pending_max_age_ms=pending_ms)) is not None
            ]
            polled = conn.execute(
                "SELECT value FROM meta WHERE key = 'last_poll_finished_at'"
            ).fetchone()
            hosts = [
                {
                    "host": str(h[0]),
                    "paused_until": h[1],
                    "last_enumerated_at": h[2],
                    "last_full_at": h[3],
                }
                for h in conn.execute(
                    "SELECT host, paused_until, last_enumerated_at, last_full_at "
                    "FROM hosts ORDER BY host"
                )
            ]
    except sqlite3.Error as exc:
        raise store_error_to_cli(classify_error(exc)) from None
    return {
        "watches": views,
        "prs": prs,
        "last_poll_finished_at": None if polled is None or not polled[0] else str(polled[0]),
        "hosts": hosts,
    }


# --- human text ----------------------------------------------------------------------------------


def _s(value: object) -> str:
    return "-" if value is None else bounded_one_line(str(value))


def _join(values: list[Any]) -> str:
    return ",".join(_s(v) for v in values) if values else "-"


def _watch_line(w: dict[str, Any]) -> str:
    subject = w["pr"] if w["kind"] == "pr" and w["pr"] is not None else w["key"]
    parts = [
        _s(w["id"]),
        _s(w["kind"]),
        _s(subject),
        f"owners={_join(w['owners'])}",
        f"labels={_join(w['labels'])}",
        f"created={_s(w['created_at'])}",
    ]
    if w["kind"] == "query":
        parts += [
            f"members={_s(w['members'])}",
            f"pending_baseline={_s(w['pending_baseline'])}",
            f"enumerated={_s(w['enumerated_at'])}",
        ]
    return "  " + "  ".join(parts)


def _pr_line(p: dict[str, Any]) -> str:
    ci = "stale" if p["ci_stale"] else _s(p["ci_state"])
    parts = [
        _s(p["pr"]),
        _s(p["status"]),
        f"state={_s(p['state'])}",
        f"ci={ci}" + (" (stuck)" if p["ci_stuck"] else ""),
        f"head={_s(p['head_sha'])[:12]}",
        f"polled={_s(p['last_polled_at'])}",
        f"failures={p['failures']}",
    ]
    if p["draft"]:
        parts.append("draft")
    if p["incomplete"]:
        parts.append(f"incomplete={_join(p['incomplete'])}({p['incomplete_snapshots']})")
    if p["terminal_kind"] is not None or p["terminal_at"] is not None:
        parts.append(f"terminal={_s(p['terminal_kind'])}@{_s(p['terminal_at'])}")
    parts.append(f"sources={_join(p['sources'])}")
    return "  " + "  ".join(parts)


def _host_line(h: dict[str, Any]) -> str:
    return (
        f"  {_s(h['host'])}  paused_until={_s(h['paused_until'])}"
        f"  last_enumerated={_s(h['last_enumerated_at'])}  last_full={_s(h['last_full_at'])}"
    )


def render(d: dict[str, Any]) -> str:
    lines = [f"watches ({len(d['watches'])})"]
    lines += [_watch_line(w) for w in d["watches"]]
    lines.append(f"prs ({len(d['prs'])})")
    lines += [_pr_line(p) for p in d["prs"]]
    lines.append(f"hosts ({len(d['hosts'])})")
    lines += [_host_line(h) for h in d["hosts"]]
    last = d["last_poll_finished_at"]
    lines.append(f"last poll finished: {'never' if last is None else _s(last)}")
    return "\n".join(lines)


def list_command(ctx: Context, args: argparse.Namespace) -> int:
    data = build(ctx, args)
    ctx.emit(data, human=render(data))
    return 0
