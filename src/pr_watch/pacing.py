"""Poll pacing: host pause, low water, the full pass and the budgets
(docs/interface.md: Pacing and backoff, Poll cycle step 4, State transaction 4 and 5).

The poll command (#25) calls, in this order:

0. At poll start (before enumeration) capture `started = time.monotonic()`: `poll.max_duration` is
   measured from the cycle's start, not from step 3.
1. `paused_hosts(conn, now)` before enumeration; pass it to `enumerate_scopes(paused_hosts=...)`.
2. `plan_hosts(conn, config, res, paused, now=..., mode=..., pr_hosts=...)` after enumeration: one
   `HostPlan` per host that may snapshot (`Pacing.plans`), to hand to `selection.choose_snapshots`
   (`force=mode.full`; a `--pr` poll builds `selection.forced_pr_task` instead and passes the
   hosts of its targets as `pr_hosts`, so a host whose only row is terminal-closed still gets a
   plan). `HostPlan.concurrency` is the one source of the pool size (`Selection.concurrency`).
3. `selection.choose_snapshots` and
   `snapshots.run_snapshots(out_of_time=time_budget(config, started=started))`.
4. `finish_host(store, config, res, pool, pacing, host, now=...)` as each host's cycle ends: right
   after enumeration for a host that runs no snapshot, and from the pool's `on_host_done` for the
   others (host state, transaction 4, each host in its own `BEGIN IMMEDIATE` that re-reads the row,
   so two polls updating one host merge instead of overwriting). A host is written once per poll;
   `finish_hosts` writes the ones still open and returns every outcome. A rate limit of the whole
   `whoami` call is `pause_hosts` (persisted at once, for every host the poll names).
5. `finish_poll(...)` writes `meta.last_poll_finished_at` (transaction 5) when the rules allow.

Judgment calls where the interface is silent (kept conservative):

- A stored `paused_until` more than an hour past `now` is invalid (a clock stepped back, a corrupt
  value; the interface never pauses longer than that): it reads as expired, the host is probed
  again and `finish_hosts` clears it. A stored `low_water_until` more than an hour ahead is read
  as `now` plus an hour (the host is called again and the reading is rewritten).
  A `full_pass_started_at` after `now` is read as `now` (a pass cannot start in the future, and
  would otherwise stay open until the clock catches up). An unparseable value reads as unset.
- A low reading with no (or an unreadable) `reset_at` limits the host for 15 minutes.
- No result from a host this cycle: its low water is left as it was (nothing was observed). A
  result with every `remaining` null clears it (a host that reports no limit is never limited).
  "Result" means contact: a successful listing, or a snapshot that committed or was discarded
  on a conflict (the signals of `observed`). A failed call, a rate-limited one included, is not
  contact: it changes neither the low water nor `pause_step`.
- A successful contact resets `pause_step` and clears an expired pause; a pause another poll set
  in the meantime (still in the future) is kept, and the later of two pauses wins.
- Host without a known backend (no `whoami` this cycle and none stored): `github.com` is GitHub,
  any other host gets the GitLab defaults (fewer workers). A snapshot carries its backend, so a
  host that only has per-PR watches (never asked by `whoami`) learns it from its first committed
  snapshot and persists it in `hosts.backend`; it is used from the next cycle's plan (this
  cycle's report and `requests_estimate` use it).
- `--pr` ignores low water (one explicit PR); a host pause still blocks it. A restricted poll
  never runs (e) or (f) work and never counts or ends a pass; it still persists a pause and a
  low-water reading, which are observations.
- `poll_count_since_full` keeps counting while a pass is open (it is reset only when a pass
  starts), so a pass that lasts `full_every` cycles or more is followed at once by the next one.
- A host that is paused, cancelled this cycle (rate limit, auth, wrong host), skipped
  (`no_credential`, `not_in_scope`) or whose `whoami` failed gets no `HostPlan`.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from . import failures as table
from .config import Config
from .enumeration import EnumerationResult, HostFacts, parse_stamp
from .refs import duration_ms
from .selection import HostPlan
from .snapshots import PoolResult
from .store import Store, format_ts

MAX_PAUSE: Final = timedelta(seconds=table.MAX_PAUSE)
LOW_WATER_NO_RESET: Final = timedelta(minutes=15)
REQUESTS_PER_SNAPSHOT: Final = {"github": 7, "gitlab": 21}


@dataclass(frozen=True)
class PollMode:
    """`full`: `--full`. `restricted`: `--pr` or `--repo` (never counts, starts or ends a pass)."""

    full: bool = False
    restricted: bool = False


@dataclass(frozen=True)
class HostState:
    """One `hosts` row, parsed; unreadable timestamps are None."""

    host: str
    backend: str | None = None
    poll_count_since_full: int = 0
    full_pass_started_at: datetime | None = None
    last_full_at: datetime | None = None
    paused_until: datetime | None = None
    pause_step: int = 0
    low_water_until: datetime | None = None


_COLUMNS: Final = (
    "host, backend, poll_count_since_full, full_pass_started_at, last_full_at, paused_until, "
    "pause_step, low_water_until"
)


def _state(row: tuple[object, ...]) -> HostState:
    count, step = row[2], row[6]
    return HostState(
        host=str(row[0]),
        backend=row[1] if row[1] in REQUESTS_PER_SNAPSHOT else None,  # type: ignore[arg-type]
        poll_count_since_full=count if isinstance(count, int) and count > 0 else 0,
        full_pass_started_at=parse_stamp(row[3]),
        last_full_at=parse_stamp(row[4]),
        paused_until=parse_stamp(row[5]),
        pause_step=step if isinstance(step, int) and step > 0 else 0,
        low_water_until=parse_stamp(row[7]),
    )


def read_states(conn: sqlite3.Connection) -> dict[str, HostState]:
    return {
        s.host: s
        for s in (_state(tuple(r)) for r in conn.execute(f"SELECT {_COLUMNS} FROM hosts"))  # noqa: S608
    }


def active_until(until: datetime | None, now: datetime) -> datetime | None:
    """A low-water `until` while it is in the future, read as at most an hour ahead; else None."""
    if until is None or until <= now:
        return None
    return min(until, now + MAX_PAUSE)


def active_pause(until: datetime | None, now: datetime) -> datetime | None:
    """A stored pause while it is in the future and at most an hour ahead. A pause further ahead
    is invalid (a clock stepped back, a corrupt row): it reads as expired, so the host is probed
    again and `finish_hosts` clears it."""
    if until is None or until <= now or until > now + MAX_PAUSE:
        return None
    return until


def concurrent_pause(until: datetime | None, now: datetime) -> datetime | None:
    """A stored pause as seen by the in-transaction merge. A poll that finished later than this one
    may have written a pause up to an hour past this poll's `now`, so up to two hours ahead is
    kept; anything further is invalid (corrupt or clock-stepped) and is overwritten or cleared."""
    if until is None or until <= now or until > now + 2 * MAX_PAUSE:
        return None
    return until


def paused_hosts(conn: sqlite3.Connection, now: datetime) -> dict[str, datetime]:
    """Hosts paused at `now` and until when (persisted, so a later process honours it)."""
    out = {}
    for host, state in read_states(conn).items():
        until = active_pause(state.paused_until, now)
        if until is not None:
            out[host] = until
    return out


def backend_of(host: str, res: EnumerationResult, state: HostState | None) -> str:
    known = _known_backend(host, res)
    if known is not None:
        return known
    if state is not None and state.backend is not None:
        return state.backend
    return "github" if host == "github.com" else "gitlab"


def _known_backend(host: str, res: EnumerationResult) -> str | None:
    """The backend `whoami` named, else the one a snapshot of the host carried this cycle."""
    listed = next((e.backend for e in res.host_list.hosts if e.host == host and e.backend), None)
    if listed is not None:
        return listed
    facts = res.hosts.get(host)
    return None if facts is None else facts.backend


@dataclass(frozen=True)
class HostPlanning:
    """What was decided for one host that was not paused at cycle start."""

    host: str
    backend: str
    full_every: int
    counted: bool  # unrestricted, and the host was listed
    start_pass: bool  # a pass starts this cycle (the stored row had none)
    pass_started_at: datetime | None  # the pass in progress (clamped), else None


@dataclass
class Pacing:
    now: datetime
    mode: PollMode
    plans: dict[str, HostPlan] = field(default_factory=dict)
    hosts: dict[str, HostPlanning] = field(default_factory=dict)
    paused: dict[str, datetime] = field(default_factory=dict)
    # hosts already written by `finish_host` (so `finish_hosts` never counts a cycle twice)
    outcomes: dict[str, HostOutcome] = field(default_factory=dict)


def _listed(facts: HostFacts | None) -> bool:
    return facts is not None and facts.listings > 0  # a failed listing is not a listing


def _blocked(facts: HostFacts | None, res: EnumerationResult, host: str) -> bool:
    if host in res.host_list.failed:
        return True
    return facts is not None and (
        facts.cancelled or facts.rate_limited or facts.skipped is not None
    )


def plan_hosts(
    conn: sqlite3.Connection,
    config: Config,
    res: EnumerationResult,
    paused: Mapping[str, datetime],
    *,
    now: datetime,
    mode: PollMode,
    pr_hosts: Collection[str] = (),
) -> Pacing:
    """Decide, per host, the plan of this cycle from the config, the stored host state and the
    enumeration facts. Writes nothing (`finish_hosts` does)."""
    states = read_states(conn)
    targets = set(pr_hosts) if mode.restricted else set()
    with_rows = {
        str(r[0])
        for r in conn.execute(
            "SELECT DISTINCT host FROM prs WHERE status IN ('active', 'unreachable')"
        )
    }
    out = Pacing(now, mode, paused=dict(paused))
    for host in sorted(with_rows | set(res.hosts) | targets):
        if host in paused:
            continue
        facts = res.hosts.get(host)
        state = states.get(host)
        backend = backend_of(host, res, state)
        settings = config.host_settings(host, "gitlab" if backend == "gitlab" else "github")
        counted = not mode.restricted and _listed(facts)
        count = (state.poll_count_since_full if state else 0) + (1 if counted else 0)
        started = state.full_pass_started_at if state else None
        started = None if started is None else min(started, now)
        start = False
        if not mode.restricted and started is None and (mode.full or count >= settings.full_every):
            start, started = True, now
        out.hosts[host] = HostPlanning(
            host, backend, settings.full_every, counted, start, None if mode.restricted else started
        )
        if _blocked(facts, res, host) or host not in with_rows | targets:
            continue
        limited = (
            not mode.full
            and active_until(state.low_water_until if state else None, now) is not None
        )
        out.plans[host] = HostPlan(
            host,
            settings.concurrency,
            0 if mode.full else config.poll.max_snapshots,
            # `--full` takes every row: nothing is done for a pass that starts now
            now if mode.full else out.hosts[host].pass_started_at,
            limited,
        )
    return out


def time_budget(
    config: Config,
    *,
    started: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> Callable[[], bool]:
    """The `out_of_time` callable of the pool: true once `poll.max_duration` has passed since
    `started` (a `time.monotonic()` reading, so a stepped wall clock changes nothing). The
    duration is measured from the start of the poll cycle: the caller captures `started` at poll
    start, before enumeration; it defaults to this call."""
    limit = duration_ms(config.poll.max_duration) / 1000
    start = monotonic() if started is None else started
    return lambda: monotonic() - start >= limit


def requests_estimate(backend: str, snapshots: int) -> int:
    """`snapshots x 7` (GitHub) or `x 21` (GitLab); an estimate (Pacing and backoff)."""
    return snapshots * REQUESTS_PER_SNAPSHOT["gitlab" if backend == "gitlab" else "github"]


# --- after the host's cycle (State, transaction 4) ---


@dataclass(frozen=True)
class HostOutcome:
    host: str
    paused_until: datetime | None  # the host is paused (this cycle or a stored pause)
    last_full_at: datetime | None
    full_pass: bool  # a pass was in progress for this host during the cycle
    full_pass_ended: bool
    limited: bool  # the host was in low water when the cycle started
    backend: str | None = None


def _contacted(host: str, res: EnumerationResult, pool: PoolResult | None) -> bool:
    """The host gave a result this cycle: a successful listing, or a snapshot that committed or
    was discarded on a conflict (the signals of `observed`). A failed call is not contact, and
    a rate-limited result is a failure."""
    facts = res.hosts.get(host)
    if facts is not None and facts.rate_limited:
        return False
    ran = pool.hosts.get(host) if pool is not None else None
    return (facts is not None and facts.listings > 0) or (
        ran is not None and (ran.snapshots > 0 or ran.skipped_conflict > 0)
    )


def _pass_done(conn: sqlite3.Connection, host: str, started: datetime) -> bool:
    """Every `active` or `unreachable` row of the host has a poll or an attempt at or after the
    pass start (a failed attempt counts)."""
    for polled, attempt in conn.execute(
        "SELECT last_polled_at, last_attempt_at FROM prs "
        "WHERE host = ? AND status IN ('active', 'unreachable')",
        (host,),
    ):
        if not any((at := parse_stamp(s)) is not None and at >= started for s in (polled, attempt)):
            return False
    return True


def _write_host(
    store: Store,
    config: Config,
    host: str,
    res: EnumerationResult,
    pool: PoolResult | None,
    pacing: Pacing,
    now: datetime,
    count_pass: bool = True,
) -> HostOutcome:
    plan = pacing.hosts[host]
    facts = res.hosts.get(host)
    contacted = _contacted(host, res, pool)
    with store.transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO hosts (host) VALUES (?)", (host,))
        row = _state(
            tuple(conn.execute(f"SELECT {_COLUMNS} FROM hosts WHERE host = ?", (host,)).fetchone())
        )  # noqa: S608
        paused_until, step = row.paused_until, row.pause_step
        if facts is not None and facts.rate_limited:
            retry = facts.retry_after
            try:
                seconds, step = table.host_pause(retry, step)
            except ValueError:
                seconds, step = table.host_pause(None, step)
            new = now + timedelta(seconds=seconds)
            current = concurrent_pause(paused_until, now)
            paused_until = new if current is None else max(new, current)
        elif concurrent_pause(paused_until, now) is None:
            paused_until = None  # expired or invalid
            step = 0 if contacted else step  # a success resets the backoff
        low = row.low_water_until
        if contacted:
            lowest = facts.lowest_remaining if facts is not None else None
            if lowest is not None and lowest < config.poll.host_low_water:
                reset = parse_stamp(facts.reset_at) if facts is not None else None
                low = reset if reset is not None else now + LOW_WATER_NO_RESET
            else:
                low = None
        count, started, last_full = (
            row.poll_count_since_full,
            row.full_pass_started_at,
            row.last_full_at,
        )
        started = None if started is None else min(started, now)
        full_pass = ended = False
        if not pacing.mode.restricted and count_pass:
            count += 1 if plan.counted else 0
            if started is None and (pacing.mode.full or count >= plan.full_every):
                started, count = pacing.now, 0
            if started is not None:
                full_pass = True
                if _pass_done(conn, host, started):
                    started, last_full, ended = None, now, True
        backend = _known_backend(host, res) or row.backend
        conn.execute(
            "UPDATE hosts SET backend = ?, poll_count_since_full = ?, full_pass_started_at = ?, "
            "last_full_at = ?, paused_until = ?, pause_step = ?, low_water_until = ? "
            "WHERE host = ?",
            (
                backend,
                count,
                None if started is None else format_ts(started),
                None if last_full is None else format_ts(last_full),
                None if paused_until is None else format_ts(paused_until),
                step,
                None if low is None else format_ts(low),
                host,
            ),
        )
    limited = pacing.plans[host].limited if host in pacing.plans else False
    return HostOutcome(
        host, active_pause(paused_until, now), last_full, full_pass, ended, limited, backend
    )


def finish_host(
    store: Store,
    config: Config,
    res: EnumerationResult,
    pool: PoolResult | None,
    pacing: Pacing,
    host: str,
    *,
    now: datetime,
    count_pass: bool = True,
) -> HostOutcome:
    """Persist one host's pause, backoff step, low water, pass counter and pass state (State,
    transaction 4) as soon as its cycle ends, so a poll killed later, or one overlapping, never
    loses a pause. Written once per poll: a second call returns the first outcome. A host paused
    at cycle start is left as it was and reported with its stored pause. A host that has no
    planning entry (seen only in `res` or `pool`) gets one of its own. `count_pass=False` (an
    interrupted poll) writes the pause and low water but leaves the pass counter and pass state
    alone."""
    done = pacing.outcomes.get(host)
    if done is not None:
        return done
    if host in pacing.paused:
        state = read_states(store.conn).get(host)
        out = HostOutcome(
            host,
            pacing.paused[host],
            None if state is None else state.last_full_at,
            False,
            False,
            False,
        )
    else:
        if host not in pacing.hosts:
            backend = backend_of(host, res, read_states(store.conn).get(host))
            settings = config.host_settings(host, "gitlab" if backend == "gitlab" else "github")
            pacing.hosts[host] = HostPlanning(
                host, backend, settings.full_every, False, False, None
            )
        out = _write_host(store, config, host, res, pool, pacing, now, count_pass)
    pacing.outcomes[host] = out
    return out


def finish_hosts(
    store: Store,
    config: Config,
    res: EnumerationResult,
    pool: PoolResult | None,
    pacing: Pacing,
    *,
    now: datetime,
) -> dict[str, HostOutcome]:
    """`finish_host` for every host not finished yet, and the outcome of every host the report
    needs. `now` is the time at the end of the cycle (a pause starts then)."""
    hosts = set(pacing.hosts) | set(res.hosts) | (set(pool.hosts) if pool is not None else set())
    hosts |= set(pacing.paused)
    return {
        host: finish_host(store, config, res, pool, pacing, host, now=now) for host in sorted(hosts)
    }


def pause_hosts(
    store: Store, hosts: Iterable[str], retry_after: float | None, *, now: datetime
) -> dict[str, datetime]:
    """Pause every host in `hosts` after a rate limit that named none (a whole `whoami` call):
    clamped like any pause, the later of a stored pause and the new one wins, `pause_step`
    advances as for a rate limit without `retry_after`. Persisted at once; returns the pause
    (`paused_until`) of each host."""
    out: dict[str, datetime] = {}
    for host in sorted(set(hosts)):
        with store.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO hosts (host) VALUES (?)", (host,))
            row = _state(
                tuple(
                    conn.execute(f"SELECT {_COLUMNS} FROM hosts WHERE host = ?", (host,)).fetchone()  # noqa: S608
                )
            )
            try:
                seconds, step = table.host_pause(retry_after, row.pause_step)
            except ValueError:
                seconds, step = table.host_pause(None, row.pause_step)
            new = now + timedelta(seconds=seconds)
            current = concurrent_pause(row.paused_until, now)
            until = new if current is None else max(new, current)
            conn.execute(
                "UPDATE hosts SET paused_until = ?, pause_step = ? WHERE host = ?",
                (format_ts(until), step, host),
            )
        out[host] = until
    return out


# --- the last step of the poll (State, transaction 5) ---


def observed(store: Store, res: EnumerationResult, pool: PoolResult | None) -> bool:
    """At least one listing or snapshot succeeded, or the store has no watches."""
    if any(f.listings > 0 for f in res.hosts.values()):
        return True
    if pool is not None and any(r.snapshots or r.skipped_conflict for r in pool.hosts.values()):
        return True
    return store.conn.execute("SELECT 1 FROM watches LIMIT 1").fetchone() is None


class _StopRequested(Exception):
    """Rolls back `finish_poll`'s transaction when the poll was stopped meanwhile."""


def finish_poll(
    store: Store,
    res: EnumerationResult,
    pool: PoolResult | None,
    *,
    now: datetime,
    restricted: bool,
    exit_ok: bool,
    interrupted: bool,
    should_stop: Callable[[], bool] = lambda: False,
) -> bool:
    """Write `meta.last_poll_finished_at` when the poll was not restricted, was not
    `already_running`, ended with exit 0, was not interrupted and observed something (`truncated`
    polls count). `should_stop` is asked again inside the transaction: a signal that arrived after
    `interrupted` was read rolls the write back. Returns whether it was written."""
    if restricted or not exit_ok or interrupted or (pool is not None and pool.already_running):
        return False
    if not observed(store, res, pool):
        return False
    try:
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('last_poll_finished_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (format_ts(now),),
            )
            if should_stop():
                raise _StopRequested
    except _StopRequested:
        return False
    return True
