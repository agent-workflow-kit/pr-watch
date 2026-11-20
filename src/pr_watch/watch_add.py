"""`watch add` (docs/interface.md: `watch add`, Watches, owners and members, PR status).

Per-PR form, in three steps:

1. **Plan** (read view, no lock): match the reference against the stored rows offline. A row with
   a digest that is not expired, or a digest-less row when `--no-baseline` is given again, needs
   no host contact. Anything else (no row, an ambiguous name, an expired row, a digest-less row to
   baseline) needs one forgectl call: `snapshot`, or `show` with `--no-baseline`. Failures end
   here through `failures.to_cli_error`, before anything is recorded.
2. **Fetch** (outside any transaction, no lease: `watch add` takes none).
3. **Write** (one `BEGIN IMMEDIATE`): the stored row is read again by the snapshot's `pr_id` and
   decides, so a poll that baselined it meanwhile wins (the fetched snapshot is discarded and only
   the source, owner and labels are added). The expiry predicate is evaluated here; an expired row
   and its per-PR watch are deleted and the PR is baselined as new. If the transaction finds it
   needs a snapshot it does not have (the row vanished or expired meanwhile) it writes nothing and
   the loop fetches once more (at most three rounds, then exit 12: a retry is safe).

The query form only records the watch; it never contacts forgectl.

Judgment calls where the interface is silent (kept conservative):

- A snapshot whose `pr` is not a valid reference on the reference's host, or whose number differs
  from the reference's, or whose `repo` is not the `pr`'s repo, is a `contract` failure.
- A snapshot over the output cap (`too_large`) ends `watch add` with exit 12 and records nothing;
  the `show` fallback of the poll is for PRs already stored. `--no-baseline` still works.
- A baseline discarded because a listing already saw a newer state of a closed PR
  (`NEWER_LISTING`) leaves the row without a digest, `baselined: false`; the next poll does it.
"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import watches
from .baseline import RowState, plan_snapshot
from .cli import CliError, Context, UsageError, _store
from .commit import commit_in_transaction
from .failures import decide, decide_tool, to_cli_error
from .forgectl_client import ForgectlClient, ToolError
from .refs import PrRef, RefError, duration_ms, watch_key
from .rows import pr_row
from .store import PR_RE, REPO_RE, NewEvent, Store

PROG_ROUNDS = 3


@dataclass
class _Outcome:
    watch: dict[str, Any]
    created: bool
    changed: bool
    baselined: bool
    events: int
    cursor: str
    pr: dict[str, Any] | None


class _NeedSnapshot(Exception):
    """The write found it needs a snapshot it was not given (nothing was written)."""


def _cursor(store: Store) -> str:
    return f"v1.{store.store_id}.{store.head_seq()}"


def _check_label_cap(conn: sqlite3.Connection, watch_id: str, args: argparse.Namespace) -> None:
    count = watches.label_count_after(conn, watch_id, args.label or [])
    if count > watches.MAX_LABELS_PER_WATCH:
        raise UsageError(
            f"a watch carries at most {watches.MAX_LABELS_PER_WATCH} labels",
            kind="too_many_labels",
            details={"limit": watches.MAX_LABELS_PER_WATCH, "count": count},
        )


def _join(conn: sqlite3.Connection, watch_id: str, args: argparse.Namespace) -> tuple[bool, bool]:
    """Owner and labels onto the watch: `(owner added, labels added)`."""
    _check_label_cap(conn, watch_id, args)
    owner = watches.add_owner(conn, watch_id, args.owner)
    labels = watches.add_labels(conn, watch_id, args.owner, args.label or [])
    return owner, labels


# --- query form ---------------------------------------------------------------------------------


def _add_query(ctx: Context, args: argparse.Namespace) -> _Outcome:
    store = _store(ctx)
    key = watch_key(args.host or [], args.repo or [])
    with store.transaction() as conn:
        cursor = _cursor(store)
        watch_id, created = watches.ensure_watch(conn, watches.QUERY_KIND, key, datetime.now(UTC))
        owner, labels = _join(conn, watch_id, args)
        view = watches.watch_view(conn, watch_id)
    assert view is not None
    return _Outcome(view, created, created or owner or labels, False, 0, cursor, None)


# --- per-PR form --------------------------------------------------------------------------------


def _snapshot_of(client: ForgectlClient, ref: PrRef, no_baseline: bool) -> dict[str, Any]:
    try:
        outcome = client.show(ref) if no_baseline else client.snapshot(ref)
    except ToolError as exc:
        raise to_cli_error(decide_tool(exc)) from None
    decision = decide(outcome, "pr")
    if decision.effect.cli_code is not None or outcome.result is None:
        raise to_cli_error(decision)
    snap = outcome.result
    pr, repo = snap.get("pr"), snap.get("repo")
    ok = (
        isinstance(pr, str)
        and isinstance(repo, str)
        and len(pr) <= 256
        and len(repo) <= 256
        and PR_RE.match(pr) is not None
        and REPO_RE.match(repo) is not None
        and pr.startswith(repo + "#")
        and pr.partition("/")[0] == ref.host
        and pr.rpartition("#")[2] == str(ref.number)
        and isinstance(snap.get("pr_id"), str)
    )
    if not ok:
        raise CliError("contract", "the snapshot does not describe the PR that was asked for")
    return snap


def _needs_snapshot(
    conn: sqlite3.Connection, ref: PrRef, cutoff: str, no_baseline: bool
) -> str | None:
    """The stored `pr_id` when no host contact is needed, else None."""
    try:
        found = watches.resolve_stored_pr(conn, ref)
    except RefError:  # an ambiguous name: the snapshot names the PR
        return None
    if found is None:
        return None
    pr_id = found[0]
    row = conn.execute(
        "SELECT digest IS NOT NULL, terminal_at FROM prs WHERE pr_id = ?", (pr_id,)
    ).fetchone()
    if row is None or watches.is_expired(row[1] if row[1] is None else str(row[1]), cutoff):
        return None
    if row[0] or no_baseline:
        return pr_id
    return None


def _write_pr(
    ctx: Context,
    args: argparse.Namespace,
    *,
    snap: dict[str, Any] | None,
    stored_pr_id: str | None,
    now: datetime,
    cutoff: str,
) -> _Outcome:
    store = _store(ctx)
    cfg = ctx.loaded.config
    pending_ms = duration_ms(cfg.ci.pending_max_age)
    pr_id = str(snap["pr_id"]) if snap is not None else stored_pr_id
    assert pr_id is not None
    with store.transaction() as conn:
        cursor = _cursor(store)
        row = conn.execute(
            "SELECT pr, repo, digest, status, terminal_at, terminal_kind, rebaseline, "
            "incomplete, baselined_at, rev FROM prs WHERE pr_id = ?",
            (pr_id,),
        ).fetchone()
        replaced = False
        if row is not None and watches.is_expired(None if row[4] is None else str(row[4]), cutoff):
            if snap is None:
                raise _NeedSnapshot
            replaced = watches.drop_expired_pr(conn, pr_id, cutoff)
            row = None
        if row is None:
            if snap is None:
                raise _NeedSnapshot
            conn.execute(
                "INSERT INTO prs (pr_id, pr, repo, host) VALUES (?, ?, ?, ?)",
                (pr_id, snap["pr"], snap["repo"], str(snap["pr"]).partition("/")[0]),
            )
            row = conn.execute(
                "SELECT pr, repo, digest, status, terminal_at, terminal_kind, rebaseline, "
                "incomplete, baselined_at, rev FROM prs WHERE pr_id = ?",
                (pr_id,),
            ).fetchone()
            created_row = True
        else:
            created_row = False
        baseline = row[2] is None and not args.no_baseline
        if baseline and snap is None:
            raise _NeedSnapshot
        watch_id, created = watches.ensure_watch(conn, watches.PR_KIND, pr_id, now)
        source = watches.add_source(conn, pr_id, watch_id)
        owner, labels = _join(conn, watch_id, args)
        baselined, emitted = False, 0
        if baseline and snap is not None:
            state = RowState(
                pr_id=pr_id,
                pr=str(row[0]),
                repo=str(row[1]),
                digest=None,
                status=str(row[3]),
                terminal_at=row[4],
                terminal_kind=row[5],
                rebaseline=bool(row[6]),
                incomplete=str(row[7]),
                baselined_at=row[8],
            )
            plan = plan_snapshot(
                state,
                snap,
                now=now,
                settle_polls=cfg.repo_settings(state.repo).settle_polls,
                persistent_after=cfg.repo_settings(state.repo).unreachable_after,
            )
            # TODO(#22): set unsettled_since for a baseline that is unsettled.
            updated = snap.get("updated_at")

            def prepare(c: sqlite3.Connection) -> list[NewEvent]:
                labels_now, ids = watches.event_context(c, pr_id)
                return [e.to_new_event(labels_now, ids) for e in plan.events]

            result = commit_in_transaction(
                store,
                conn,
                pr_id=pr_id,
                expected_rev=int(row[9]),
                snapshot_pr_id=str(snap["pr_id"]),
                fields=plan.fields,
                prepare=prepare,
                snapshot_updated_at=updated if isinstance(updated, str) else "",
            )
            baselined, emitted = result.committed, len(result.events)
        view = watches.watch_view(conn, watch_id)
        prrow = pr_row(conn, pr_id, now=now, pending_max_age_ms=pending_ms)
    assert view is not None
    changed = created_row or replaced or created or source or owner or labels or baselined
    return _Outcome(view, created, changed, baselined, emitted, cursor, prrow)


def _add_pr(ctx: Context, args: argparse.Namespace) -> _Outcome:
    store = _store(ctx)
    ref = args.pr
    assert isinstance(ref, PrRef)
    client: ForgectlClient | None = None
    snap: dict[str, Any] | None = None
    for _ in range(PROG_ROUNDS):
        now = datetime.now(UTC)
        cutoff = watches.expiry_cutoff(now, ctx.loaded.config.expiry.after_terminal)
        stored = _needs_snapshot(store.conn, ref, cutoff, args.no_baseline)
        if stored is None and snap is None:
            try:
                client = client or ForgectlClient.from_config(ctx.loaded.config.forgectl)
            except ToolError as exc:
                raise to_cli_error(decide_tool(exc)) from None
            snap = _snapshot_of(client, ref, args.no_baseline)
        try:
            return _write_pr(ctx, args, snap=snap, stored_pr_id=stored, now=now, cutoff=cutoff)
        except _NeedSnapshot:
            continue
    raise CliError("host_failed", "the PR changed while it was being added; retry")


def _human(args: argparse.Namespace, out: _Outcome) -> str:
    target = out.watch["key"] if out.pr is None else out.pr["pr"]
    if not out.changed:
        return f"{args.owner} already watches {target}"
    text = f"{args.owner} now watches {target}"
    if out.baselined:
        text += f" (baselined, {out.events} event{'s' if out.events != 1 else ''})"
    return text


def watch_add(ctx: Context, args: argparse.Namespace) -> int:
    out = _add_query(ctx, args) if args.pr is None else _add_pr(ctx, args)
    ctx.emit(
        {
            "watch": out.watch,
            "created": out.created,
            "changed": out.changed,
            "owner": args.owner,
            "baselined": out.baselined,
            "events_emitted": out.events,
            "cursor": out.cursor,
            "pr": out.pr,
        },
        human=_human(args, out),
    )
    return 0
