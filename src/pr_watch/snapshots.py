"""Poll cycle steps 5 and 6: the snapshot pool and the per-PR commit pipeline
(docs/interface.md: Poll cycle, Locks and commit, Pacing and backoff, Failures, Signals).

`run_snapshots` takes the `Selection` of `selection.py` and runs it:

- **Threads.** Worker threads only call forgectl and build the plan (`plan_snapshot` is pure); the
  store, the leases and the log are used from the calling thread alone (one SQLite connection).
  Each host has its own bounded pool (`HostPlan.concurrency` workers in flight, so memory is
  bounded by a small multiple of it); hosts run in parallel.
- **Lease per PR**, taken when the snapshot starts and released at its commit or failure, renewed
  when the forgectl call returned. A PR whose lease a live other poll holds is `skipped_locked`.
  With the lease held the row is read again and the trigger checked on it (`selection.still_due`):
  a PR another poll committed in between is not snapshotted twice; a forced task is never skipped.
- **Commit order.** Results of one host are committed in the order the PRs were started, so the
  event stream is deterministic for a given selection. A result waits for the ones started
  before it (at most `2 x concurrency` are started ahead of the oldest uncommitted one).
- **Commit** is `commit.commit_snapshot` (compare-and-set). Inside its transaction each event's
  labels and watches are read first, then the membership rule removes the query sources that left
  (the snapshot shows `open`, `left_count` 2), so the events of a PR that loses its last source
  still name that source. A label union over the store's 256 is cut to the first 256 in sorted
  order and, when the snapshot has events, logged as `labels_capped`; diff drops are logged as
  one `events_dropped` record per snapshot.
- **Failures** (`failures.decide`): counted failures are written in their own transaction
  (`failure_count`, `last_failure_code`, `last_attempt_at`, `status` unreachable at the threshold)
  and never produce an event; a rate limit or whole-host failure cancels the host's queue and
  changes no PR, and is recognised as soon as its call returns, not when its turn to commit comes
  (a call that is already running still finishes); a tool failure stops the cycle. An exception
  in a worker is an `internal` counted failure of that PR only.
- **Stopping.** `should_stop` (a signal) ends the loop at once: nothing more is committed, the
  leases are released and `interrupted` is set; PRs in flight keep their persisted triggers.
  `out_of_time` (the `poll.max_duration` budget, `pacing.time_budget`) only stops starting: the
  rest is `deferred` and `truncated` is set. The per-host cap is applied by `selection.py`.
- **Host end.** `on_host_done(host, out)` is called from the calling thread when a host's queue
  and in-flight entries have all ended (every result committed or failed), so the poll writes
  that host's state (pause, low water, pass) before the other hosts' long phases; never after a
  signal or a tool failure halted the pool.

Judgment calls where the interface is silent (kept conservative):

- `snapshots` counts committed snapshots; a discarded commit counts only as a conflict, and a
  call that failed counts in `calls`.
- A snapshot whose `updated_at` is not a readable timestamp is a `contract` failure (the commit
  needs it for the closed-snapshot check and for `snap_updated_at`).
- A host-level failure marks the host in `EnumerationResult.failures` once per cycle; its
  in-flight PRs still commit what they fetched.

- **Over the cap.** A `too_large` snapshot is followed, in the same worker, by one
  `forgectl show` (`_fallback`): a `merged` or `closed` PR commits its one terminal event through
  `baseline.plan_overcap`; an open one is the counted `too_large` failure; a failing `show` is
  classified by `failures.decide` itself (never a second `show`); another `pr_id` is
  `pr_id_changed`, and an open one is `too_large`, even with an unreadable `updated_at`; only a
  terminal `show` needs it (the commit requires `snapshot_updated_at`), and there an unreadable
  one is a `contract` failure. The job counts
  two calls in `calls`.
- **Force push.** `compare_forced_for(client)` (the default `forced_for`) gives the diff a check
  that calls `forgectl compare --no-diff` (only the status is read) when the repo's
  `detect_force_push` is true, in the worker thread, from the data read at lease time. A sha that
  is not 40-hex (a 64-hex sha-256 one included) gets no call (`forgectl compare` takes 40-hex
  only; `forced` is null); any failure, a raising client included, is `forced: null` and never a
  PR failure.
Host pause, low water and `last_full_at` are written by the pacing stage (`pacing.py`).
"""

from __future__ import annotations

import re
import sqlite3
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from . import failures as table
from . import settle
from .baseline import (
    MalformedSnapshot,
    PrIdMismatch,
    RowState,
    SnapshotPlan,
    plan_overcap,
    plan_snapshot,
)
from .commit import CommitResult, DiscardReason, commit_snapshot
from .config import Config
from .diff import Drop, ForcedCheck, forced_from_compare
from .digest import digest_from_json
from .enumeration import EnumerationResult, Failure, note_rate, parse_stamp, record_failure
from .forgectl_client import ForgectlClient, Outcome, ToolError
from .leases import Lease, Leases
from .log import Log
from .refs import duration_ms, parse_pr
from .renames import rename_event
from .selection import Selection, Task, still_due
from .store import MAX_LABELS, EventError, NewEvent, Store, StoredEvent, StoreError, format_ts
from .watches import event_context

WINDOW_FACTOR = 2
WAIT_SECONDS = 0.2

ForcedFor = Callable[["RowStart"], ForcedCheck | None]
_SHA1 = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class RowStart:
    """The row as it was when the snapshot started (`rev` is what the commit compares)."""

    state: RowState
    rev: int
    listed_updated_at: str | None
    unsettled_since: str | None
    failure_count: int
    last_failure_code: str | None
    settle_polls: int
    unreachable_after: int
    detect_force_push: bool


@dataclass
class HostRun:
    calls: int = 0
    snapshots: int = 0  # committed
    unchanged: int = 0  # committed with no event
    events: int = 0
    skipped_locked: int = 0
    skipped_conflict: int = 0
    deferred: int = 0


@dataclass
class PoolResult:
    hosts: dict[str, HostRun] = field(default_factory=dict)
    events: list[StoredEvent] = field(default_factory=list)
    unreachable: list[tuple[str, str, str]] = field(default_factory=list)  # pr, pr_id, code
    needed: int = 0
    interrupted: bool = False
    truncated: bool = False
    tool_error: ToolError | None = None

    def host(self, name: str) -> HostRun:
        return self.hosts.setdefault(name, HostRun())

    @property
    def skipped_locked(self) -> int:
        return sum(h.skipped_locked for h in self.hosts.values())

    @property
    def deferred(self) -> int:
        return sum(h.deferred for h in self.hosts.values())

    @property
    def already_running(self) -> bool:
        """At least one PR needed a snapshot and every such PR was held by a live other poll."""
        return self.needed > 0 and self.skipped_locked == self.needed


@dataclass(frozen=True)
class _Prepared:
    plan: SnapshotPlan
    fields: dict[str, Any]
    snapshot_pr_id: str
    updated_at: str


@dataclass(frozen=True)
class _Job:
    outcome: Outcome | None = None
    decision: table.Decision | None = None
    prepared: _Prepared | None = None
    tool: ToolError | None = None
    calls: int = 1  # forgectl calls the job made (a fallback job made two)


@dataclass
class _Entry:
    task: Task
    start: RowStart
    lease: Lease
    future: Future[_Job]
    renewed: bool = False
    classified: bool = False


@dataclass
class _Host:
    name: str
    concurrency: int
    queue: deque[Task]
    entries: deque[_Entry] = field(default_factory=deque)
    cancelled: bool = False
    noted: bool = False

    def inflight(self) -> int:
        return sum(1 for e in self.entries if not e.future.done())


# --- the worker: forgectl and the pure plan ---


def _local(decision: table.Decision) -> _Job:
    return _Job(decision=decision)


def _work(
    client: ForgectlClient,
    task: Task,
    start: RowStart,
    now: datetime,
    forced_for: ForcedFor | None,
    should_stop: Callable[[], bool] | None = None,
) -> _Job:
    """One snapshot call and its plan. Never raises: a failure is a decision. After a signal no
    follow-up call (`show`, `compare`) is started: the pool discards the result anyway."""

    def stopped() -> bool:
        return should_stop is not None and should_stop()

    try:
        outcome = client.snapshot(parse_pr(start.state.pr))
    except ToolError as exc:
        return _Job(tool=exc)
    except Exception:
        return _local(table.decide_code("internal", None, "pr"))
    decision = table.decide(outcome, "pr")
    if stopped():
        return _local(table.decision_for("interrupted", "pr"))
    if decision.effect.fallback_show:
        return _fallback(client, start, now, decision)
    if not decision.effect.commit or outcome.result is None:
        return _Job(outcome, decision)
    snap = outcome.result
    try:
        updated = parse_stamp(snap.get("updated_at"))
        if updated is None:
            return _Job(outcome, table.decision_for("contract", "pr"))
        forced = forced_for(start) if forced_for is not None and not stopped() else None
        plan = plan_snapshot(
            start.state,
            snap,
            now=now,
            forced=forced,
            settle_polls=start.settle_polls,
            persistent_after=start.unreachable_after,
        )
        fields = {
            **plan.fields,
            **settle.snapshot_columns(
                stored=_stored_digest(start),
                new=plan.digest,
                snapshot=snap,
                unsettled_since=start.unsettled_since,
                listed_updated_at=start.listed_updated_at,
                now=now,
            ),
        }
        return _Job(
            outcome, decision, _Prepared(plan, fields, str(snap["pr_id"]), format_ts(updated))
        )
    except PrIdMismatch:
        return _Job(outcome, table.decide_local("pr_id_changed", "pr"))
    except MalformedSnapshot:
        return _Job(outcome, table.decision_for("contract", "pr"))
    except Exception:
        return _Job(outcome, table.decide_code("internal", None, "pr"))


def _fallback(
    client: ForgectlClient, start: RowStart, now: datetime, too_large: table.Decision
) -> _Job:
    """The over-cap fallback: one `show` for terminal-state detection only. Never raises. The
    job counts two calls (the `too_large` snapshot and the `show`)."""
    return replace(_show_fallback(client, start, now, too_large), calls=2)


def _show_fallback(
    client: ForgectlClient, start: RowStart, now: datetime, too_large: table.Decision
) -> _Job:
    """The `show` half of `_fallback`. `plan_overcap` runs first (the `pr_id` check, and open ->
    None), so another PR is `pr_id_changed` and an open one the `too_large` failure even when
    `updated_at` is unreadable; only a terminal `show` needs `updated_at` (the commit requires
    it), and an unreadable one there is a `contract` failure."""
    try:
        outcome = client.show(parse_pr(start.state.pr))
    except ToolError as exc:
        return _Job(tool=exc)
    except Exception:
        return _local(table.decide_code("internal", None, "pr"))
    decision = table.decide(outcome, "pr")  # a failing show is classified like any PR call
    if not decision.effect.commit or outcome.result is None:
        return _Job(outcome, decision)
    shown = outcome.result
    try:
        plan = plan_overcap(start.state, shown, now=now)
        if plan is None:
            return _Job(outcome, too_large)  # still open (or unreadable): the counted failure
        updated = parse_stamp(shown.get("updated_at"))
        if updated is None:
            return _Job(outcome, table.decision_for("contract", "pr"))
        fields = {
            **plan.fields,
            **settle.snapshot_columns(
                stored=_stored_digest(start),
                new=plan.digest,
                snapshot=shown,
                unsettled_since=start.unsettled_since,
                listed_updated_at=start.listed_updated_at,
                now=now,
            ),
        }
        return _Job(
            outcome, decision, _Prepared(plan, fields, str(shown["pr_id"]), format_ts(updated))
        )
    except PrIdMismatch:
        return _Job(outcome, table.decide_local("pr_id_changed", "pr"))
    except MalformedSnapshot:
        return _Job(outcome, table.decision_for("contract", "pr"))
    except Exception:
        return _Job(outcome, table.decide_code("internal", None, "pr"))


def compare_forced_for(client: ForgectlClient) -> ForcedFor:
    """The default `forced_for`: `forced` from `forgectl compare --no-diff` (see the module
    docstring). It runs in a worker thread and is given the row data only, never the store. A
    failed compare is forced null, a vanished tool included: the next call stops the poll."""

    def forced_for(start: RowStart) -> ForcedCheck | None:
        if not start.detect_force_push:
            return None

        def check(from_sha: str, to_sha: str) -> bool | None:
            if not (_SHA1.fullmatch(from_sha) and _SHA1.fullmatch(to_sha)):
                return None  # forgectl compare takes 40-hex only (sha-256 pushes: unknown)
            try:
                outcome = client.compare(parse_pr(start.state.pr), from_sha, to_sha, no_diff=True)
            except Exception:
                return None  # a failed call is `forced: null`, never a failed snapshot
            result = outcome.result
            if outcome.local is not None or result is None or outcome.returncode != 0:
                return None
            return forced_from_compare(result.get("status"))

        return check

    return forced_for


def _stored_digest(start: RowStart) -> dict[str, Any] | None:
    return digest_from_json(start.state.digest)


# --- the pool ---


class _Pool:
    def __init__(
        self,
        store: Store,
        client: ForgectlClient,
        config: Config,
        selection: Selection,
        res: EnumerationResult,
        *,
        log: Log | None,
        leases: Leases,
        clock: Callable[[], datetime],
        should_stop: Callable[[], bool] | None,
        out_of_time: Callable[[], bool] | None,
        forced_for: ForcedFor | None,
        on_host_done: Callable[[str, PoolResult], None] | None = None,
        on_events: Callable[[Sequence[StoredEvent]], None] | None = None,
    ) -> None:
        self.on_host_done = on_host_done
        self.on_events = on_events
        self.done: set[str] = set()
        self.store, self.client, self.config, self.res = store, client, config, res
        self.log, self.leases, self.clock = log, leases, clock
        self.should_stop, self.out_of_time = should_stop, out_of_time
        self.forced_for = forced_for if forced_for is not None else compare_forced_for(client)
        self.out = PoolResult(needed=selection.total)
        self.hosts = {
            host: _Host(host, max(1, selection.concurrency.get(host, 1)), deque(tasks))
            for host, tasks in sorted(selection.tasks.items())
            if tasks
        }
        for host in selection.tasks:
            self.out.host(host)
        self.halt = False

    # -- loop --

    def run(self) -> PoolResult:
        workers = max(1, sum(h.concurrency for h in self.hosts.values()))
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pr-watch-snap")
        try:
            while not self.halt:
                if self.should_stop is not None and self.should_stop():
                    self.out.interrupted = True
                    break
                self._classify()
                for host in self.hosts.values():
                    self._fill(executor, host)
                self._renew()
                for host in self.hosts.values():
                    self._drain(host)
                    if self.halt:
                        break
                self._notify_done()
                if all(not h.queue and not h.entries for h in self.hosts.values()):
                    break
                pending = [e.future for h in self.hosts.values() for e in h.entries]
                pending = [f for f in pending if not f.done()]
                if pending:
                    wait(pending, timeout=WAIT_SECONDS, return_when=FIRST_COMPLETED)
        finally:
            self._release_all()
            executor.shutdown(wait=True)
        return self.out

    def _notify_done(self) -> None:
        """Tell the caller each host whose queue and entries have ended (State transaction 4 is
        written per host as it ends). Never after a halt: that host's cycle was cut off."""
        if self.on_host_done is None or self.halt:
            return
        for host in self.hosts.values():
            if host.name not in self.done and not host.queue and not host.entries:
                self.done.add(host.name)
                self.on_host_done(host.name, self.out)

    def _release_all(self) -> None:
        for host in self.hosts.values():
            for entry in host.entries:
                self._release(entry)
            host.entries.clear()

    def _release(self, entry: _Entry) -> None:
        self._release_lease(entry.lease)

    def _release_lease(self, lease: Lease) -> None:
        try:
            self.leases.release(lease)
        except StoreError:
            pass  # a lease that cannot be released expires; nothing else to do

    def _classify(self) -> None:
        """Stop a host at once when a finished call shows a whole-host failure (rate limit, auth):
        results are committed in order, but the failure must not wait for its turn while the pool
        keeps calling the host. The failure itself is recorded when the entry is finished."""
        for host in self.hosts.values():
            if host.cancelled:
                continue
            for entry in host.entries:
                if not entry.future.done() or entry.classified:
                    continue
                entry.classified = True
                decision = entry.future.result().decision
                if decision is None or decision.effect.stop_poll:
                    continue
                if decision.effect.pause != "none" or decision.effect.cancel_host:
                    host.cancelled = True
                    host.queue.clear()
                    break

    # -- starting --

    def _fill(self, executor: ThreadPoolExecutor, host: _Host) -> None:
        run = self.out.host(host.name)
        while host.queue and not host.cancelled:
            if len(host.entries) >= WINDOW_FACTOR * host.concurrency or (
                host.inflight() >= host.concurrency
            ):
                return
            if self.out_of_time is not None and self.out_of_time():
                self.out.truncated = True
                run.deferred += len(host.queue)
                host.queue.clear()
                return
            task = host.queue.popleft()
            lease = self.leases.acquire(task.pr_id)
            if lease is None:
                run.skipped_locked += 1
                continue
            try:
                start = self._load(task)
                if start is None or not self._due(task):
                    self._release_lease(lease)
                    continue
                future = executor.submit(
                    _work, self.client, task, start, self.clock(), self.forced_for,
                    self.should_stop,
                )  # fmt: skip
            except BaseException:
                self._release_lease(lease)  # not in `host.entries` yet: `_release_all` misses it
                raise
            host.entries.append(_Entry(task, start, lease, future))
        if host.cancelled:
            host.queue.clear()

    def _due(self, task: Task) -> bool:
        """The task's trigger on the row as it is now, with the lease held."""
        return still_due(
            self.store.conn, task, now=self.clock(),
            pending_max_age_ms=duration_ms(self.config.ci.pending_max_age),
        )  # fmt: skip

    def _load(self, task: Task) -> RowStart | None:
        """The row as the snapshot starts, or None when it is gone or no longer eligible."""
        r = self.store.conn.execute(
            "SELECT pr, repo, digest, status, terminal_at, terminal_kind, rebaseline, incomplete, "
            "baselined_at, rev, listed_updated_at, unsettled_since, failure_count, "
            "last_failure_code FROM prs WHERE pr_id = ?",
            (task.pr_id,),
        ).fetchone()
        if r is None or (r[3] == "terminal" and not (task.forced and r[5] != "merged")):
            return None  # a forced task (`--pr`) may reopen a terminal-closed row
        settings = self.config.repo_settings(str(r[1]))
        state = RowState(
            pr_id=task.pr_id,
            pr=str(r[0]),
            repo=str(r[1]),
            digest=r[2],
            status=str(r[3]),
            terminal_at=r[4],
            terminal_kind=r[5],
            rebaseline=bool(r[6]),
            incomplete=str(r[7]),
            baselined_at=r[8],
            opened_since=self._opened_since(task.pr_id) if r[2] is None else None,
        )
        return RowStart(
            state, int(r[9]), r[10], r[11], int(r[12]), r[13], settings.settle_polls,
            settings.unreachable_after, settings.detect_force_push,
        )  # fmt: skip

    def _opened_since(self, pr_id: str) -> str | None:
        """The creation time of the oldest query watch that holds the PR, for `opened`. None when
        none does, when a per-PR watch names the PR (the caller named it: never `opened`), or when
        the events table still holds any event for the PR (a returning row: its `opened` or later
        events are retained, so a leave-and-rejoin or a new row for a closed PR emits no second).
        `events_by_pr` (pr_id, seq) makes the lookup an index probe."""
        row = self.store.conn.execute(
            "SELECT min(w.created_at) FROM sources s JOIN watches w ON w.id = s.watch_id "
            "WHERE s.pr_id = ? AND w.kind = 'query' "
            "AND NOT EXISTS (SELECT 1 FROM watches p WHERE p.kind = 'pr' AND p.key = ?) "
            "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.pr_id = ?)",
            (pr_id, pr_id, pr_id),
        ).fetchone()
        return None if row is None or row[0] is None else str(row[0])

    def _renew(self) -> None:
        for host in self.hosts.values():
            for entry in host.entries:
                if entry.future.done() and not entry.renewed:
                    entry.renewed = True
                    renewed = self.leases.renew(entry.lease)
                    if renewed is not None:
                        entry.lease = renewed

    # -- finishing --

    def _drain(self, host: _Host) -> None:
        while host.entries and host.entries[0].future.done() and not self.halt:
            if self.should_stop is not None and self.should_stop():
                self.out.interrupted = True
                self.halt = True
                return
            entry = host.entries.popleft()
            try:
                self._finish(host, entry)
            finally:
                self._release(entry)

    def _finish(self, host: _Host, entry: _Entry) -> None:
        run = self.out.host(host.name)
        job = entry.future.result()
        run.calls += job.calls
        if job.tool is not None:
            self.out.tool_error = job.tool
            self.halt = True
            return
        if job.outcome is not None and job.outcome.result is not None:
            facts = self.res.facts(host.name)
            note_rate(facts, job.outcome.result)
            backend = job.outcome.result.get("backend")
            if backend in ("github", "gitlab"):
                facts.backend = backend  # a host that only has per-PR watches learns it here
        if job.prepared is not None:
            self._commit(host, entry, job.prepared)
        else:
            assert job.decision is not None
            self._fail(host, entry, job.decision)

    def _commit(self, host: _Host, entry: _Entry, p: _Prepared) -> None:
        capped: tuple[int, int] | None = None  # (kept, total)
        rename_drops: list[Drop] = []
        fields = dict(p.fields)  # `prepare` may drop the name columns (stale plan)

        def prepare(conn: sqlite3.Connection) -> Sequence[NewEvent]:
            nonlocal capped
            # read before the membership rule below removes a source (the last one included)
            labels, ids = event_context(conn, entry.task.pr_id)
            # `renamed` is read against the name the row holds now, inside this transaction, so a
            # rename a listing (or another poll) stored meanwhile is not reported twice; it
            # comes first in the snapshot's events. The name is applied only while the row still
            # has the one the plan saw: a snapshot taken under an older name must neither undo a
            # newer rename nor report it backwards.
            rename = drop = None
            if "pr" in p.fields or "repo" in p.fields:
                now_name = conn.execute(
                    "SELECT pr, repo FROM prs WHERE pr_id = ?", (entry.task.pr_id,)
                ).fetchone()
                planned = (entry.start.state.pr, entry.start.state.repo)
                if now_name is not None and (now_name[0], now_name[1]) != planned:
                    fields.pop("pr", None)
                    fields.pop("repo", None)
                else:
                    rename, drop = rename_event(
                        conn, entry.task.pr_id, p.fields.get("pr"), p.fields.get("repo"),
                        head_sha=p.plan.digest.get("head_sha"),
                    )  # fmt: skip
            found = ([rename] if rename is not None else []) + list(p.plan.events)
            if drop is not None:
                rename_drops.append(drop)
            if p.plan.digest.get("state") == "open":
                conn.execute(
                    "DELETE FROM sources WHERE pr_id = ? AND left_count >= 2 AND watch_id IN "
                    "(SELECT id FROM watches WHERE kind = 'query')",
                    (entry.task.pr_id,),
                )
            if found and len(labels) > MAX_LABELS:
                capped, labels = (MAX_LABELS, len(labels)), labels[:MAX_LABELS]
            return [e.to_new_event(labels, ids) for e in found]

        try:
            result = commit_snapshot(
                self.store,
                pr_id=entry.task.pr_id,
                expected_rev=entry.start.rev,
                snapshot_pr_id=p.snapshot_pr_id,
                fields=fields,
                prepare=prepare,
                snapshot_updated_at=p.updated_at,
            )
        except EventError:
            self._fail(host, entry, table.decision_for("contract", "pr"))
            return
        pr = str(fields.get("pr", entry.start.state.pr))
        if not result.committed:
            self._discarded(host, entry, result)
            return
        self._committed(host, entry, p, result, pr, capped, tuple(rename_drops))

    def _committed(
        self,
        host: _Host,
        entry: _Entry,
        p: _Prepared,
        result: CommitResult,
        pr: str,
        capped: tuple[int, int] | None,
        extra_drops: tuple[Drop, ...] = (),
    ) -> None:
        run = self.out.host(host.name)
        run.snapshots += 1
        run.events += len(result.events)
        run.unchanged += 0 if result.events else 1
        self.out.events.extend(result.events)
        if result.events and self.on_events is not None:
            self.on_events(result.events)  # committed: hooks may be queued (never before)
        pr_id = entry.task.pr_id
        for part in p.plan.persistent:
            self._log("part_incomplete", pr_id=pr_id, pr=pr, part=part,
                      snapshots=p.plan.incomplete[part])  # fmt: skip
        drops = (*p.plan.events.dropped, *extra_drops)
        if drops:
            self._log("events_dropped", "warn", pr=pr, pr_id=pr_id, count=len(drops),
                      reasons=sorted({d.reason for d in drops}))  # fmt: skip
        if capped is not None:
            self._log("labels_capped", "warn", pr=pr, pr_id=pr_id, kept=capped[0],
                      total=capped[1])  # fmt: skip
        before, after = entry.start.state.status, p.fields.get("status", entry.start.state.status)
        if before != after:
            self._log("pr_status", pr=pr, pr_id=pr_id, **{"from": before, "to": after})

    def _discarded(self, host: _Host, entry: _Entry, result: CommitResult) -> None:
        reason = result.reason
        assert reason is not None
        if reason is DiscardReason.PR_ID_MISMATCH:
            self._fail(host, entry, table.decide_local("pr_id_changed", "pr"))
            return
        if result.counts_as_conflict:
            self.out.host(host.name).skipped_conflict += 1
        self._log("commit_skipped", "warn", pr_id=entry.task.pr_id, reason=reason.value)

    def _fail(self, host: _Host, entry: _Entry, decision: table.Decision) -> None:
        effect = decision.effect
        code = decision.code or "internal"
        pr = entry.start.state.pr
        if effect.stop_poll:
            self.halt = True
            return
        if effect.pause != "none" or effect.cancel_host:
            host.cancelled = True
            host.queue.clear()
            if not host.noted:
                host.noted = True
                record_failure(
                    self.res, self.res.facts(host.name), decision, kind_ref=host.name, log=self.log
                )
            return
        if effect.counter == "increment":
            self._count(entry, decision)
        if effect.failed_kind == "pr":
            self.res.failures.append(Failure("pr", pr, code, effect.poll_exit))

    def _count(self, entry: _Entry, decision: table.Decision) -> None:
        start, effect = entry.start, decision.effect
        code = decision.code or "internal"
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT failure_count, status, last_failure_code FROM prs WHERE pr_id = ?",
                (entry.task.pr_id,),
            ).fetchone()
            if row is None:
                return
            count, unreachable = table.apply_counter(
                effect, int(row[0]), start.unreachable_after, str(row[1])
            )
            conn.execute(
                "UPDATE prs SET failure_count = ?, last_failure_code = ?, last_attempt_at = ?"
                + (", status = 'unreachable'" if unreachable else "")
                + " WHERE pr_id = ?",
                (count, code, format_ts(self.clock()), entry.task.pr_id),
            )
        pr = start.state.pr
        if effect.record and table.should_record(row[2], code, count, start.unreachable_after):
            self._log("snapshot_failed", "warn", pr=pr, pr_id=entry.task.pr_id, code=code,
                      failures=count)  # fmt: skip
        if unreachable:
            self.out.unreachable.append((pr, entry.task.pr_id, code))
            self._log("pr_status", pr=pr, pr_id=entry.task.pr_id,
                      **{"from": "active", "to": "unreachable"})  # fmt: skip

    def _log(self, event: str, level: str = "info", **fields: Any) -> None:
        if self.log is not None:
            self.log.write(event, level, **fields)


def make_leases(store: Store, config: Config) -> Leases:
    """The leases of one poll: a ttl of `3 x forgectl.timeout` (6 minutes by default), at least
    two calls of one worker (the over-cap fallback makes two, each up to the timeout plus the
    10 s deadline slack) and 30 s to spare."""
    timeout_ms = duration_ms(config.forgectl.timeout)
    two_calls_ms = 2 * (timeout_ms + 10_000) + 30_000
    return Leases(store, timedelta(milliseconds=max(3 * timeout_ms, two_calls_ms)))


def run_snapshots(
    store: Store,
    client: ForgectlClient,
    config: Config,
    selection: Selection,
    res: EnumerationResult,
    *,
    leases: Leases,
    log: Log | None = None,
    clock: Callable[[], datetime] | None = None,
    should_stop: Callable[[], bool] | None = None,
    out_of_time: Callable[[], bool] | None = None,
    forced_for: ForcedFor | None = None,
    on_host_done: Callable[[str, PoolResult], None] | None = None,
    on_events: Callable[[Sequence[StoredEvent]], None] | None = None,
) -> PoolResult:
    """Run the chosen snapshots (see the module docstring). Failures and host facts are added to
    `res`; the counters and the committed events are returned."""
    return _Pool(
        store, client, config, selection, res,
        log=log, leases=leases, clock=clock or (lambda: datetime.now(UTC)),
        should_stop=should_stop, out_of_time=out_of_time, forced_for=forced_for,
        on_host_done=on_host_done, on_events=on_events,
    ).run()  # fmt: skip
