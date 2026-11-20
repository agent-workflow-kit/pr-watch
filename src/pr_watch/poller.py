"""The `poll` command: one cycle, end to end (docs/interface.md: Poll cycle, Signals, `poll`).

The stages live in their own modules; this one wires them in the documented order and owns what
belongs to no stage: the signal contract, the report, the exit code, the log records of a poll
(`poll_start`, `host_summary`, `poll_end`) and the daily housekeeping.

1. Preflight: forgectl is resolved and version-checked once (`ForgectlClient.from_config`,
   `.version`); its path and version go into `poll_start`. A missing or too old forgectl is the
   exit 12 envelope (`tool_missing`, `tool_too_old`) before any work.
2. `pacing.paused_hosts`, then `enumeration.enumerate_scopes` (steps 1 to 3), or nothing for
   `--pr`. A whole-`whoami` rate limit pauses every host of the cached or known host list.
3. `pacing.plan_hosts`, `selection.choose_snapshots` (or `forced_pr_task` for `--pr`). A host that
   will run no snapshot is finished at once (`pacing.finish_host`: its pause, low water and pass
   state are persisted before any later long phase).
4. `snapshots.run_snapshots`, with the cycle's start as the `poll.max_duration` clock. Each host's
   state is written the moment its queue ends (`on_host_done`), not at the end of the pool.
5. `pacing.finish_hosts` (the rest), `pacing.finish_poll` (`meta.last_poll_finished_at`), the
   report, `poll_end`, and at most once a day `prune.prune`.

Signals. SIGTERM, SIGINT and SIGHUP set a stop flag (`_Signals`) and SIGTERM the forgectl process
groups the poll started (`gateway.signal_live`); the stages poll the flag, so nothing more is
started, nothing in flight is committed, the pool releases its leases, `poll_end` carries
`interrupted: true` and the exit is 128 plus the number, with no report. The handlers are
restored afterwards. A forgectl child that exits 128+N while the flag is clear is an interrupted PR
(`failures`); the flag is what tells the two apart.

Judgment calls where the interface is silent (kept conservative):

- `--pr` with `--repo` is a usage error (`bad_combination`, in `cli.py`); `--full` with either is
  allowed (it forces the snapshots of the scope that is covered).
- `--pr` of a PR that is not stored is `not_found` (exit 10), checked before forgectl is touched.
- `polled` (with `--pr`) is true when that PR's snapshot committed. A failed, locked, conflicting,
  deferred, paused or merged PR is `polled: false`.
- A `whoami` failure with no cached host list (query watches skipped) is reported in `failed[]`
  as kind `host` with `ref` `whoami`; it makes exit 12 whenever the failure table says so (a rate
  limit does not). With a stale cache the poll goes on, logs the warning and the `host_failed`
  records, prints one warning line on stderr and is not a failure by itself (exit 0).
- A rate limit of the whole `whoami` call pauses every host of the host list in use (the stale
  cache or the fresh list), the hosts already stored and the hosts that have PR rows; a `--repo`
  poll pauses only its repo's host. The pause is persisted at once, clamped like any pause.
- `hosts.<h>.snapshots` is the number of committed snapshots and `requests_estimate` is that times
  7 or 21; `hosts.<h>.errors` holds the listing and whole-host errors (`pr` null), per-PR
  failures are in `failed[]` only.
- With `already_running` every counter but `prs.skipped_locked` is zero, `prs.incomplete`
  included (the schema says only `skipped_locked` can be non-zero).
- Housekeeping runs at the end of any poll that was not interrupted, when `last_prune_at` is
  missing, unreadable, in the future or at least a day old. A failure of it never changes the
  poll's result (one warning line on stderr).
- Hooks (`on-<event>`, `post-poll`, `--hook`, `--no-hooks`) are the seams marked TODO(#28); the
  report counts them as zero.
"""

from __future__ import annotations

import argparse
import signal
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import FrameType
from typing import Any, Final

from . import gateway, prune, watches
from . import pacing as pc
from .cli import CliError, Context, _stderr_line, _store
from .config import Config
from .enumeration import (
    EnumerationResult,
    Failure,
    HostFacts,
    HostList,
    enumerate_scopes,
    parse_stamp,
)
from .failures import decide_tool, to_cli_error
from .forgectl_client import ForgectlClient, ToolError, ToolVersion
from .refs import EVENT_TYPES, PrId, duration_ms
from .selection import Selection, Task, choose_snapshots, forced_pr_task
from .snapshots import PoolResult, make_leases, run_snapshots
from .store import Store, StoreError, format_ts

PROG: Final = "pr-watch"
PRUNE_EVERY: Final = timedelta(days=1)
WHOAMI_REF: Final = "whoami"  # `failed[].ref` of a `whoami` call that names no host
MAX_STDERR_ERRORS: Final = 50


# --- signals ---------------------------------------------------------------------------------


class _Signals:
    """Context manager: SIGTERM, SIGINT and SIGHUP set `signum` and signal the forgectl children
    (never raise), so every stage can finish its transaction and stop. Handlers are replaced and
    restored only on the main thread (the only place Python delivers them)."""

    def __init__(self) -> None:
        self.signum: int | None = None
        self._saved: dict[int, Any] = {}

    def __enter__(self) -> _Signals:
        if threading.current_thread() is threading.main_thread():
            for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                self._saved[number] = signal.signal(number, self._handle)
        return self

    def __exit__(self, *exc: object) -> None:
        for number, old in self._saved.items():
            signal.signal(number, old)
        self._saved.clear()

    def _handle(self, number: int, frame: FrameType | None) -> None:
        if self.signum is None:
            self.signum = number
        gateway.signal_live(signal.SIGTERM)

    def stopped(self) -> bool:
        return self.signum is not None


# --- the run -----------------------------------------------------------------------------------


@dataclass
class _Run:
    """Everything the report and `poll_end` are built from."""

    ctx: Context
    args: argparse.Namespace
    store: Store
    config: Config
    sig: _Signals
    started: datetime
    clock0: float
    res: EnumerationResult = field(
        default_factory=lambda: EnumerationResult(HostList((), frozenset(), True))
    )
    pool: PoolResult | None = None
    pacing: pc.Pacing | None = None
    outcomes: dict[str, pc.HostOutcome] = field(default_factory=dict)
    selection: Selection = field(default_factory=Selection)
    pr_task: Task | None = None
    pr_host: str | None = None
    exit_code: int = 0
    ended: bool = False
    version: ToolVersion | None = None

    @property
    def restricted(self) -> bool:
        return self.args.pr is not None or self.args.repo is not None

    @property
    def repo_host(self) -> str | None:
        return None if self.args.repo is None else self.args.repo.partition("/")[0].lower()

    @property
    def interrupted(self) -> bool:
        return (
            self.sig.stopped()
            or self.res.interrupted
            or (self.pool is not None and self.pool.interrupted)
        )

    def now(self) -> datetime:
        return datetime.now(UTC)


def _preflight(ctx: Context) -> tuple[ForgectlClient, ToolVersion]:
    try:
        client = ForgectlClient.from_config(ctx.loaded.config.forgectl)
        return client, client.version()
    except ToolError as exc:
        raise to_cli_error(decide_tool(exc)) from None


def _resolve_pr(store: Store, args: argparse.Namespace) -> tuple[str, str]:
    """`(pr_id, host)` of the `--pr` target; `not_found` when it is not stored."""
    found = watches.resolve_stored_pr(store.conn, args.pr)
    if found is None:
        raise CliError(
            "not_found", "no stored PR matches the reference", details={"kind": "pr_unknown"}
        )
    host = store.conn.execute("SELECT host FROM prs WHERE pr_id = ?", (found[0],)).fetchone()
    return found[0], str(host[0])


def _known_hosts(run: _Run) -> set[str]:
    """The hosts a whole-`whoami` rate limit pauses: the host list in use, the stored host rows
    and the hosts with PR rows (a `--repo` poll: its own host only)."""
    conn = run.store.conn
    hosts = {e.host for e in run.res.host_list.hosts}
    hosts |= {str(r[0]) for r in conn.execute("SELECT host FROM hosts")}
    hosts |= {str(r[0]) for r in conn.execute("SELECT DISTINCT host FROM prs")}
    if run.repo_host is not None:
        hosts &= {run.repo_host}
    return hosts


def _enumerate(run: _Run, client: ForgectlClient, paused: dict[str, datetime]) -> None:
    args = run.args
    try:
        run.res = enumerate_scopes(
            run.store, client, run.config, now=run.started, log=run.ctx.log,
            paused_hosts=set(paused), restrict_repo=args.repo, should_stop=run.sig.stopped,
        )  # fmt: skip
    except ToolError as exc:
        raise to_cli_error(decide_tool(exc)) from None
    if run.res.whoami_rate_limited and not run.res.interrupted:
        pause = pc.pause_hosts(
            run.store, _known_hosts(run), run.res.whoami_retry_after, now=run.now()
        )
        paused.update(pause)


def _plan(run: _Run, paused: dict[str, datetime]) -> None:
    conn, config, args = run.store.conn, run.config, run.args
    mode = pc.PollMode(full=args.full, restricted=run.restricted)
    pr_hosts = [] if run.pr_host is None else [run.pr_host]
    run.pacing = pacing = pc.plan_hosts(
        conn, config, run.res, paused, now=run.started, mode=mode, pr_hosts=pr_hosts
    )
    if run.res.stop_poll:
        return  # forgectl's own config_invalid: no snapshot is started
    if run.pr_host is not None:
        plan = pacing.plans.get(run.pr_host)
        if run.pr_task is not None and plan is not None:
            run.selection = Selection(
                tasks={run.pr_host: [run.pr_task]}, concurrency={run.pr_host: plan.concurrency}
            )
        return
    only = None
    if args.repo is not None:
        only = {
            str(r[0])
            for r in conn.execute(
                "SELECT pr_id FROM prs WHERE lower(repo) = ?", (args.repo.lower(),)
            )
        }
    run.selection = choose_snapshots(
        conn, pacing.plans, now=run.started,
        pending_max_age_ms=duration_ms(config.ci.pending_max_age),
        only_pr_ids=only, force=args.full,
    )  # fmt: skip


def _finish_idle_hosts(run: _Run) -> None:
    """Persist the state of every host that runs no snapshot this cycle (a rate limit found by
    enumeration, a paused or failed host, a host with nothing chosen) before the pool starts."""
    assert run.pacing is not None
    busy = {host for host, tasks in run.selection.tasks.items() if tasks}
    hosts = set(run.pacing.hosts) | set(run.res.hosts) | set(run.pacing.paused)
    for host in sorted(hosts - busy):
        pc.finish_host(run.store, run.config, run.res, None, run.pacing, host, now=run.now())


def _snapshot(run: _Run, client: ForgectlClient) -> None:
    assert run.pacing is not None
    pacing = run.pacing

    def host_done(host: str, out: PoolResult) -> None:
        pc.finish_host(run.store, run.config, run.res, out, pacing, host, now=run.now())

    run.pool = run_snapshots(
        run.store, client, run.config, run.selection, run.res,
        leases=make_leases(run.store, run.config), log=run.ctx.log,
        should_stop=run.sig.stopped,
        out_of_time=pc.time_budget(run.config, started=run.clock0),
        on_host_done=host_done,
    )  # fmt: skip


# --- the report --------------------------------------------------------------------------------


def _failed(res: EnumerationResult) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for f in res.failures:
        entry = {"kind": f.kind, "ref": f.ref if f.ref is not None else WHOAMI_REF, "code": f.code}
        if entry not in out:
            out.append(entry)
    return out


def _incomplete_count(store: Store) -> int:
    row = store.conn.execute(
        "SELECT count(*) FROM prs WHERE incomplete IS NOT NULL AND incomplete NOT IN ('', '{}')"
    ).fetchone()
    return int(row[0])


def _host_names(run: _Run) -> list[str]:
    names = set(run.outcomes) | set(run.res.hosts)
    if run.pool is not None:
        names |= set(run.pool.hosts)
    if run.pacing is not None:
        names |= set(run.pacing.paused)
    if run.pr_host is not None:
        names = {run.pr_host}
    elif run.repo_host is not None:
        names = {run.repo_host}
    return sorted(names)


def _host_entry(run: _Run, host: str) -> dict[str, Any]:
    facts = run.res.hosts.get(host) or HostFacts(host)
    ran = run.pool.hosts.get(host) if run.pool is not None else None
    outcome = run.outcomes.get(host)
    snapshots = ran.snapshots if ran is not None else 0
    backend = (outcome.backend if outcome is not None else None) or pc.backend_of(
        host, run.res, None
    )

    def stamp(value: datetime | None) -> str | None:
        return None if value is None else format_ts(value)

    return {
        "queried": facts.queried,
        "snapshots": snapshots,
        "requests_estimate": pc.requests_estimate(backend, snapshots),
        "paused_until": stamp(outcome.paused_until if outcome is not None else None),
        "skipped": facts.skipped,
        "incomplete_listing": facts.incomplete_listing,
        "errors": [
            {"code": e.code, "pr": None, "retry_after": e.retry_after} for e in facts.errors
        ],
        "last_full_at": stamp(outcome.last_full_at if outcome is not None else None),
    }


def _events_by_type(pool: PoolResult | None) -> dict[str, int]:
    counts: dict[str, int] = {t: 0 for t in EVENT_TYPES}  # noqa: C420 (typed as str keys)
    for event in pool.events if pool is not None else ():
        if event.type in counts:
            counts[event.type] += 1
    return counts


def build_report(run: _Run) -> dict[str, Any]:
    """The poll report (schemas/cli/poll.json)."""
    pool = run.pool or PoolResult()
    already = pool.already_running
    truncated = pool.truncated or run.selection.total_deferred > 0
    hosts = {h: _host_entry(run, h) for h in _host_names(run)}
    report: dict[str, Any] = {
        "already_running": already,
        "started_at": format_ts(run.started),
        "duration_ms": int((time.monotonic() - run.clock0) * 1000),
        "hosts": hosts,
        "prs": {
            "snapshotted": sum(r.snapshots for r in pool.hosts.values()),
            "unchanged": sum(r.unchanged for r in pool.hosts.values()),
            "skipped_locked": pool.skipped_locked,
            "skipped_conflict": sum(r.skipped_conflict for r in pool.hosts.values()),
            "unreachable": len(pool.unreachable),
            "incomplete": 0 if already else _incomplete_count(run.store),
        },
        "events": _events_by_type(pool),
        "hooks": {"run": 0, "skipped": 0, "failed": 0},  # TODO(#28): hook counts
        "failed": _failed(run.res),
        "full_pass": any(o.full_pass for o in run.outcomes.values()),
        "truncated": truncated,
        "deferred": run.selection.total_deferred + pool.deferred,
    }
    if run.args.pr is not None:
        report["polled"] = run.pr_task is not None and any(
            r.snapshots > 0 for r in pool.hosts.values()
        )
    return report


def _human(report: dict[str, Any]) -> str:
    prs, failed = report["prs"], report["failed"]
    lines = [
        f"polled {prs['snapshotted']} PRs ({prs['unchanged']} unchanged), "
        f"{sum(report['events'].values())} events, {len(failed)} failed, "
        f"{report['duration_ms']} ms"
    ]
    if report["already_running"]:
        lines.append("another poll is running: nothing was snapshotted")
    if "polled" in report and not report["polled"]:
        lines.append("the PR was not refreshed")
    for host, h in report["hosts"].items():
        facts = [f"{h['snapshots']} snapshots"]
        if h["skipped"]:
            facts.append(f"skipped ({h['skipped']})")
        if h["paused_until"]:
            facts.append(f"paused until {h['paused_until']}")
        if h["incomplete_listing"]:
            facts.append("listing incomplete")
        lines.append(f"  {host}: " + ", ".join(facts))
    if report["truncated"]:
        lines.append(f"stopped early: {report['deferred']} snapshots deferred")
    if report["full_pass"]:
        lines.append("a full pass ran")
    lines.extend(f"failed: {f['kind']} {f['ref']} ({f['code']})" for f in failed)
    return "\n".join(lines)


# --- housekeeping and records ------------------------------------------------------------------


def _housekeeping(run: _Run) -> None:
    """Prune once a day, at the end of a poll that ran to the end."""
    store, config = run.store, run.config
    try:
        row = store.conn.execute("SELECT value FROM meta WHERE key = 'last_prune_at'").fetchone()
        last = parse_stamp(row[0]) if row is not None else None
        now = run.now()
        if last is not None and last <= now < last + PRUNE_EVERY:
            return
        prune.prune(store, now, config.retention.events, config.expiry.after_terminal)
    except StoreError:
        _stderr_line(f"{PROG}: warning: housekeeping (prune) failed; it runs again next poll\n")


def _write_end(run: _Run, report: dict[str, Any] | None) -> None:
    if run.ended or run.version is None:  # no `poll_start` was written: no `poll_end`
        return
    run.ended = True
    log = run.ctx.log
    pool = run.pool
    snapshots = 0 if pool is None else sum(r.snapshots for r in pool.hosts.values())
    events = 0 if pool is None else len(pool.events)
    log.write(
        "poll_end",
        "info",
        duration_ms=int((time.monotonic() - run.clock0) * 1000),
        snapshots=snapshots,
        events=events,
        errors=len(_failed(run.res)),
        already_running=False if pool is None else pool.already_running,
        interrupted=run.interrupted,
        truncated=bool(report["truncated"]) if report is not None else False,
    )


def _host_summaries(run: _Run, report: dict[str, Any]) -> None:
    for host, entry in report["hosts"].items():
        run.ctx.log.write(
            "host_summary", "info", host=host, snapshots=entry["snapshots"],
            requests_estimate=entry["requests_estimate"], errors=len(entry["errors"]),
            paused_until=entry["paused_until"],
        )  # fmt: skip


def _stderr_lines(run: _Run, exit_code: int) -> None:
    for warning in run.res.warnings:
        _stderr_line(f"{PROG}: warning: {warning}\n")
    if exit_code == 12:
        failures: Iterable[Failure] = [f for f in run.res.failures if f.poll_exit == 12]
        for f in list(failures)[:MAX_STDERR_ERRORS]:
            _stderr_line(f"{PROG}: error: {f.kind} {f.ref or WHOAMI_REF} failed ({f.code})\n")


# --- the command -------------------------------------------------------------------------------


def poll_command(ctx: Context, args: argparse.Namespace) -> int:
    store = _store(ctx)
    with _Signals() as sig:
        run = _Run(ctx, args, store, ctx.loaded.config, sig, datetime.now(UTC), time.monotonic())
        try:
            return _poll(run)
        finally:
            _write_end(run, None)  # only reached unwritten on an error or an interruption


def _poll(run: _Run) -> int:
    ctx, args, store = run.ctx, run.args, run.store
    if args.pr is not None:
        pr_id, run.pr_host = _resolve_pr(store, args)
        run.pr_task = forced_pr_task(store.conn, pr_id)
    client, run.version = _preflight(ctx)
    scope = (
        None
        if args.pr is None and args.repo is None
        else (
            args.pr.canonical
            if args.pr is not None and not isinstance(args.pr, PrId)
            else args.repo
        )
    )
    start_fields: dict[str, Any] = {} if scope is None else {"scope": str(scope)}
    ctx.log.write(
        "poll_start", "info", full=args.full, forgectl_path=client.exe,
        forgectl_version=run.version.text, **start_fields,
    )  # fmt: skip
    paused = pc.paused_hosts(store.conn, run.started)
    if args.pr is None:
        _enumerate(run, client, paused)
    if run.interrupted:
        return 128 + (run.sig.signum or signal.SIGTERM)
    _plan(run, paused)
    _finish_idle_hosts(run)
    if run.selection.total and not run.res.stop_poll:
        _snapshot(run, client)
        assert run.pool is not None
        if run.pool.tool_error is not None:
            raise to_cli_error(decide_tool(run.pool.tool_error))
    if run.interrupted:
        return 128 + (run.sig.signum or signal.SIGTERM)
    assert run.pacing is not None
    run.outcomes = pc.finish_hosts(store, run.config, run.res, run.pool, run.pacing, now=run.now())
    exit_code = 12 if run.res.stop_poll or any(f.poll_exit == 12 for f in run.res.failures) else 0
    pc.finish_poll(
        store, run.res, run.pool, now=run.now(), restricted=run.restricted,
        exit_ok=exit_code == 0, interrupted=False,
    )  # fmt: skip
    report = build_report(run)
    _host_summaries(run, report)
    _stderr_lines(run, exit_code)
    ctx.emit(report, human=_human(report))
    # TODO(#28): run the on-<event> hooks (queued after each PR's commit) and post-poll here, with
    # `args.no_hooks` and `args.hook`; fill `report["hooks"]` before it is printed.
    _write_end(run, report)
    _housekeeping(run)
    return exit_code
