"""View model of `browse` (docs/interface.md, TUI): reads and shapes the stored state as plain,
already sanitized text. It needs no Textual, so it is tested on its own.

Reads reuse the code of `list`, `show` and `events`, through the shared read-only open: nothing is
written or created, no host is contacted, and a missing store is an empty one. Each call opens the
state directory and `state.db` afresh and closes them again, so a store that appears (or breaks)
between two reads is seen as it is.

Judgment calls where the interface is silent:

- The events view is the most recent `EVENT_WINDOW` stored events, newest first. `events` itself
  needs a cursor and returns nothing without one, so the window is read directly (still the
  validated `stored_event` shape; a malformed stored event fails the whole read, as in `events`).
- The watches and PRs of a snapshot come from one read transaction (`list`'s), the events from a
  second one, and a PR's detail (`show`'s text) from a third when it is selected.
- A PR's labels are the union of the labels of the watches that source it (the union an event
  carries); the status filter applies to PRs only and is ignored for watches and events.
- Every displayed string is passed through `bounded_one_line`, or `scrub_text` for the multi-line
  details, so a stored control, bidirectional or invisible character becomes a `<U+XXXX>` marker.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from . import listing, show
from .cli import CliError, Context, ref_error_to_cli, store_error_to_cli
from .config import LoadedConfig
from .events import read_snapshot, stored_event, summary
from .refs import PrId, PrRef, RefError
from .state_dir import StateDirError, open_existing_state_dir
from .store import Store, StoreError, classify_error, open_existing_store
from .text import bounded_one_line, scrub_text
from .watches import resolve_stored_pr

EVENT_WINDOW: Final = 200
BUSY_TIMEOUT_MS: Final = 1000  # a locked store is a stale view, not a frozen screen
VIEWS: Final = ("prs", "watches", "events")
STATUSES: Final = ("active", "terminal", "unreachable")
COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "prs": ("PR", "STATUS", "STATE", "CI", "FAIL", "POLLED"),
    "watches": ("ID", "KIND", "SUBJECT", "OWNERS", "LABELS", "MEMBERS"),
    "events": ("SEQ", "DETECTED", "TYPE", "PR", "SUMMARY", "LABELS"),
}
_EVENT_SQL: Final = (
    "SELECT seq, id, type, pr_id, pr, payload, detected_at, labels FROM events "
    "ORDER BY seq DESC LIMIT ?"
)


@dataclass(frozen=True)
class Item:
    """One table row: its stable `key`, sanitized `cells` and `detail`, and what filters match."""

    key: str
    cells: tuple[str, ...]
    detail: str
    status: str | None
    labels: tuple[str, ...]
    haystack: str


@dataclass(frozen=True)
class Snapshot:
    prs: tuple[Item, ...] = ()
    watches: tuple[Item, ...] = ()
    events: tuple[Item, ...] = ()
    hosts: tuple[str, ...] = ()
    last_poll: str | None = None
    read_at: datetime | None = None
    empty: bool = True

    def items(self, view: str) -> tuple[Item, ...]:
        return {"prs": self.prs, "watches": self.watches, "events": self.events}[view]


def _s(value: object) -> str:
    return "-" if value is None else bounded_one_line(str(value))


def _join(values: Iterable[object]) -> str:
    return ",".join(_s(v) for v in values) or "-"


def _item(
    key: str,
    cells: Iterable[str],
    detail: str,
    status: str | None,
    labels: Iterable[str],
    extra: Iterable[str] = (),
) -> Item:
    cell_t = tuple(cells)
    label_t = tuple(labels)
    haystack = " ".join([*cell_t, *label_t, *extra]).lower()
    return Item(key, cell_t, scrub_text(detail), status, label_t, haystack)


# --- reading -------------------------------------------------------------------------------------


@contextmanager
def _context(loaded: LoadedConfig) -> Iterator[Context]:
    """A read-only context: the state dir and store are opened without creating anything."""
    try:
        state = open_existing_state_dir()
    except StateDirError as exc:
        raise CliError("state_unsafe", str(exc)) from None
    store: Store | None = None
    try:
        if state is not None:
            try:
                store = open_existing_store(state, busy_timeout_ms=BUSY_TIMEOUT_MS)
            except StateDirError as exc:
                raise CliError("state_unsafe", str(exc)) from None
            except StoreError as exc:
                raise store_error_to_cli(exc) from None
        yield Context(
            json=False, quiet=True, verbose=False, loaded=loaded, state=state, store=store
        )
    finally:
        if store is not None:
            store.close()
        if state is not None:
            state.close()


def _list_args() -> argparse.Namespace:
    return argparse.Namespace(owner=None, label=None, pr=None, status=None)


def _recent_events(ctx: Context) -> list[dict[str, Any]]:
    if ctx.store is None:
        return []
    conn = ctx.store.conn
    try:
        with read_snapshot(conn):
            rows = conn.execute(_EVENT_SQL, (EVENT_WINDOW,)).fetchall()
    except sqlite3.Error as exc:
        raise store_error_to_cli(classify_error(exc)) from None
    return [stored_event(r).to_json() for r in rows]


def load_snapshot(loaded: LoadedConfig, *, now: datetime | None = None) -> Snapshot:
    """Everything the three views show. Raises `CliError` (state_unsafe, store_too_new, internal)
    when the store cannot be read; the caller keeps its last snapshot and marks it stale."""
    now = datetime.now(UTC) if now is None else now
    with _context(loaded) as ctx:
        data = listing.build(ctx, _list_args(), now=now)
        events = _recent_events(ctx)
    watch_labels = {w["id"]: list(w["labels"]) for w in data["watches"]}
    prs = tuple(_pr_item(p, watch_labels) for p in data["prs"])
    pr_names: dict[str, list[str]] = {}
    for p in data["prs"]:
        for source in p["sources"]:
            pr_names.setdefault(source, []).append(p["pr"])
    return Snapshot(
        prs=prs,
        watches=tuple(_watch_item(w, pr_names.get(w["id"], [])) for w in data["watches"]),
        events=tuple(_event_item(e) for e in events),
        hosts=tuple(_host_line(h) for h in data["hosts"]),
        last_poll=None
        if data["last_poll_finished_at"] is None
        else _s(data["last_poll_finished_at"]),
        read_at=now,
        empty=not (data["watches"] or data["prs"] or events),
    )


def load_pr_detail(loaded: LoadedConfig, pr_id: str, *, now: datetime | None = None) -> str | None:
    """The text of `show` for one stored PR, or None when it is no longer stored."""
    now = datetime.now(UTC) if now is None else now
    with _context(loaded) as ctx:
        try:
            data = show.build(ctx, argparse.Namespace(pr=PrId(pr_id)), now=now)
        except CliError as exc:
            if exc.code == "not_found":
                return None
            raise
    return scrub_text(show.render(data))


def resolve_focus(loaded: LoadedConfig, ref: PrRef | PrId) -> str | None:
    """The `pr_id` of the stored PR a reference names, or None (an empty store names nothing).
    An ambiguous reference is the usage error `show` gives."""
    with _context(loaded) as ctx:
        if ctx.store is None:
            return None
        try:
            with read_snapshot(ctx.store.conn):
                found = resolve_stored_pr(ctx.store.conn, ref)
        except RefError as exc:
            raise ref_error_to_cli(exc) from None
        except sqlite3.Error as exc:
            raise store_error_to_cli(classify_error(exc)) from None
    return None if found is None else found[0]


# --- items ---------------------------------------------------------------------------------------


def _pr_item(p: dict[str, Any], watch_labels: dict[str, list[str]]) -> Item:
    ci = "stale" if p["ci_stale"] else _s(p["ci_state"])
    if p["ci_stuck"]:
        ci += " (stuck)"
    labels = sorted({_s(x) for w in p["sources"] for x in watch_labels.get(w, [])})
    cells = (
        _s(p["pr"]),
        _s(p["status"]),
        _s(p["state"]) + (" draft" if p["draft"] else ""),
        ci,
        str(p["failures"]),
        _s(p["last_polled_at"]),
    )
    detail = "\n".join(
        [
            f"{cells[0]}  {cells[1]}  state={cells[2]}  ci={ci}",
            f"head={_s(p['head_sha'])[:12]}  polled={cells[5]}  failures={cells[4]}",
            f"sources={_join(p['sources'])}  labels={_join(labels)}",
        ]
    )
    return _item(p["pr_id"], cells, detail, str(p["status"]), labels, p["sources"])


def _watch_item(w: dict[str, Any], pr_names: list[str]) -> Item:
    subject = w["pr"] if w["kind"] == "pr" and w["pr"] is not None else w["key"]
    cells = (
        _s(w["id"]),
        _s(w["kind"]),
        _s(subject),
        _join(w["owners"]),
        _join(w["labels"]),
        _s(w["members"]),
    )
    lines = [
        f"watch {cells[0]}  kind={cells[1]}",
        f"subject: {cells[2]}",
        f"owners: {cells[3]}",
        f"labels: {cells[4]}",
        f"created: {_s(w['created_at'])}",
    ]
    if w["kind"] == "query":
        lines.append(
            f"members: {cells[5]}  pending_baseline={_s(w['pending_baseline'])}"
            f"  enumerated={_s(w['enumerated_at'])}"
        )
    lines.append(f"prs ({len(pr_names)})")
    lines += [f"  {_s(n)}" for n in sorted(pr_names)]
    return _item(w["id"], cells, "\n".join(lines), None, [_s(x) for x in w["labels"]], w["owners"])


def _event_item(e: dict[str, Any]) -> Item:
    labels = [_s(x) for x in e.get("labels", [])]
    cells = (
        _s(e.get("seq")),
        _s(e.get("detected_at")),
        _s(e.get("type")),
        _s(e.get("pr")),
        summary(e),
        _join(labels) if labels else "-",
    )
    # JSON with ASCII escapes: every stored character that is not printable ASCII is escaped.
    return _item(str(e["seq"]), cells, json.dumps(e, indent=2, ensure_ascii=True), None, labels)


def _host_line(h: dict[str, Any]) -> str:
    return (
        f"{_s(h['host'])}  paused_until={_s(h['paused_until'])}"
        f"  last_enumerated={_s(h['last_enumerated_at'])}  last_full={_s(h['last_full_at'])}"
    )


# --- filters and age -----------------------------------------------------------------------------


def visible(
    items: Iterable[Item], *, text: str = "", status: str | None = None, label: str | None = None
) -> list[Item]:
    """The items matching every active filter. `text` is a case-insensitive substring of the row
    (cells, labels, sources); `status` only narrows items that have one (PRs)."""
    needle = text.strip().lower()
    return [
        i
        for i in items
        if (not needle or needle in i.haystack)
        and (status is None or i.status is None or i.status == status)
        and (label is None or label in i.labels)
    ]


def label_choices(items: Iterable[Item]) -> list[str]:
    return sorted({lbl for i in items for lbl in i.labels})


def age_text(then: datetime, now: datetime) -> str:
    """`45s`, `10m`, `6h` or `5d`: how long ago `then` was (never negative)."""
    seconds = max(0, int((now - then).total_seconds()))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"
