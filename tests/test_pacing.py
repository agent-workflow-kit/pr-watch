"""Pacing: host pause, low water, full pass, budgets (#23)."""
# ruff: noqa: F811 (pytest fixtures imported from test_snapshots)

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pr_watch import enumeration as en
from pr_watch import pacing as pc
from pr_watch import selection as sel
from pr_watch import snapshots as sn
from pr_watch.config import Config
from pr_watch.state_dir import open_state_dir
from pr_watch.store import Store, format_ts, open_store
from tests.forgectl_support import FakeForgectl
from tests.test_snapshots import (  # noqa: F401
    HOST,
    baseline,
    col,
    cycle,
    fake,
    leases,
    make_client,
    pr,
    seed,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
GL = "gitlab.example.net"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with open_state_dir(tmp_path / "pr-watch") as s, open_store(s) as st:
        yield st


def empty() -> en.EnumerationResult:
    return en.EnumerationResult(en.HostList((), frozenset(), True))


def row(store: Store, host: str = HOST) -> dict[str, Any]:
    cur = store.conn.execute("SELECT * FROM hosts WHERE host = ?", (host,))
    names = [d[0] for d in cur.description]
    r = cur.fetchone()
    return dict(zip(names, r, strict=True)) if r else {}


def add_pr(store: Store, n: int, host: str = HOST, **cols: Any) -> str:
    pr_id = f"id-{host}-{n}"
    store.conn.execute(
        "INSERT INTO prs (pr_id, pr, repo, host) VALUES (?, ?, ?, ?)",
        (pr_id, f"{host}/o/r#{n}", f"{host}/o/r", host),
    )
    for k, v in cols.items():
        store.conn.execute(f"UPDATE prs SET {k} = ? WHERE pr_id = ?", (v, pr_id))  # noqa: S608
    store.conn.commit()
    return pr_id


def facts(res: en.EnumerationResult, host: str = HOST, **kw: Any) -> en.HostFacts:
    f = res.facts(host)
    f.listings = kw.pop("listings", 1)
    for k, v in kw.items():
        setattr(f, k, v)
    return f


def run(store, res, *, now=NOW, mode=None, pool=None, config=None, pr_hosts=()):
    cfg = config or Config()
    mode = mode or pc.PollMode()
    paused = pc.paused_hosts(store.conn, now)
    pacing = pc.plan_hosts(store.conn, cfg, res, paused, now=now, mode=mode, pr_hosts=pr_hosts)
    return pacing, lambda at=now: pc.finish_hosts(store, cfg, res, pool, pacing, now=at)


# --- low water ---


def test_null_rate_limit_never_limits_a_host(store: Store) -> None:
    add_pr(store, 1, GL)
    store.conn.execute(
        "INSERT INTO hosts (host, low_water_until) VALUES (?, ?)",
        (GL, format_ts(NOW + timedelta(minutes=5))),
    )
    store.conn.commit()
    res = empty()
    f = facts(res, GL)
    en.note_rate(f, {"rate_limit": {"remaining": None, "reset_at": None}})  # no limit headers
    assert f.lowest_remaining is None
    pacing, finish = run(store, res)
    assert pacing.plans[GL].limited  # the stored limit holds at cycle start
    finish()
    assert row(store, GL)["low_water_until"] is None
    pacing2, _ = run(store, empty())
    assert not pacing2.plans[GL].limited


def test_low_reading_limits_until_reset_and_full_overrides(store: Store) -> None:
    add_pr(store, 1)
    res = empty()
    reset = NOW + timedelta(minutes=20)
    facts(res, lowest_remaining=99, reset_at=format_ts(reset))
    _, finish = run(store, res)
    finish()
    assert row(store)["low_water_until"] == format_ts(reset)
    pacing, _ = run(store, empty(), now=NOW + timedelta(minutes=1))
    assert pacing.plans[HOST].limited
    full, _ = run(store, empty(), now=NOW + timedelta(minutes=1), mode=pc.PollMode(full=True))
    assert not full.plans[HOST].limited and full.plans[HOST].max_snapshots == 0
    after, _ = run(store, empty(), now=reset + timedelta(seconds=1))
    assert not after.plans[HOST].limited


def test_reading_at_threshold_clears_and_no_contact_keeps(store: Store) -> None:
    add_pr(store, 1)
    store.conn.execute(
        "INSERT INTO hosts (host, low_water_until) VALUES (?, ?)",
        (HOST, format_ts(NOW + timedelta(minutes=5))),
    )
    store.conn.commit()
    _, finish = run(store, empty())  # nothing called
    finish()
    assert row(store)["low_water_until"] is not None
    res = empty()
    facts(res, lowest_remaining=100)
    _, finish = run(store, res)
    finish()
    assert row(store)["low_water_until"] is None


def test_stored_low_water_far_ahead_still_limits_and_is_rewritten_by_the_next_contact(
    store: Store,
) -> None:
    add_pr(store, 1)
    store.conn.execute(
        "INSERT INTO hosts (host, low_water_until) VALUES (?, ?)",
        (HOST, format_ts(NOW + timedelta(days=3))),
    )
    store.conn.commit()
    res = empty()
    facts(res, lowest_remaining=5)  # low again, no reset_at
    pacing, finish = run(store, res)
    assert pacing.plans[HOST].limited  # clamped on read, not dropped
    finish()
    assert row(store)["low_water_until"] == format_ts(NOW + timedelta(minutes=15))


def test_stored_pause_more_than_an_hour_ahead_is_invalid_and_expired(store: Store) -> None:
    add_pr(store, 1)
    store.conn.execute(
        "INSERT INTO hosts (host, paused_until, pause_step) VALUES (?, ?, 240)",
        (HOST, format_ts(NOW + timedelta(days=1))),
    )
    store.conn.commit()
    paused = pc.paused_hosts(store.conn, NOW)
    assert paused == {}
    res = empty()
    facts(res)
    pacing, finish = run(store, res)
    assert HOST in pacing.plans  # the host is probed again
    out = finish()
    assert row(store)["paused_until"] is None and row(store)["pause_step"] == 0
    assert out[HOST].paused_until is None


def test_stored_pause_up_to_an_hour_ahead_still_pauses(store: Store) -> None:
    store.conn.execute(
        "INSERT INTO hosts (host, paused_until) VALUES (?, ?)",
        (HOST, format_ts(NOW + timedelta(hours=1))),
    )
    store.conn.commit()
    assert pc.paused_hosts(store.conn, NOW) == {HOST: NOW + timedelta(hours=1)}
    assert pc.paused_hosts(store.conn, NOW + timedelta(hours=1)) == {}


def test_invalid_far_pause_is_cleared_even_when_the_host_was_not_contacted(store: Store) -> None:
    add_pr(store, 1)
    store.conn.execute(
        "INSERT INTO hosts (host, paused_until) VALUES (?, ?)",
        (HOST, format_ts(NOW + timedelta(days=9999))),
    )
    store.conn.commit()
    _, finish = run(store, empty())
    finish()
    assert row(store)["paused_until"] is None


# --- pause ---


def test_retry_after_of_a_day_pauses_one_hour_and_all_paused_keeps_last_poll(store: Store) -> None:
    add_pr(store, 1)
    res = empty()
    facts(res, listings=0, rate_limited=True, retry_after=86400.0, cancelled=True)
    _, finish = run(store, res)
    out = finish()
    assert out[HOST].paused_until == NOW + timedelta(hours=1)
    assert row(store)["pause_step"] == 0
    # next process: paused, skipped, no plan, nothing observed
    later = NOW + timedelta(minutes=5)
    paused = pc.paused_hosts(store.conn, later)
    assert HOST in paused
    pacing = pc.plan_hosts(store.conn, Config(), empty(), paused, now=later, mode=pc.PollMode())
    assert (
        pacing.plans == {}
        and pc.finish_hosts(store, Config(), empty(), None, pacing, now=later)[HOST].paused_until
    )
    store.conn.execute(
        "INSERT INTO watches VALUES ('w1', 'pr', 'k', '2026-01-01T00:00:00.000000Z')"
    )
    store.conn.execute("INSERT INTO meta (key, value) VALUES ('last_poll_finished_at', 'old')")
    store.conn.commit()
    wrote = pc.finish_poll(
        store, empty(), None, now=later, restricted=False, exit_ok=True, interrupted=False
    )
    assert not wrote
    assert (
        store.conn.execute("SELECT value FROM meta WHERE key='last_poll_finished_at'").fetchone()[0]
        == "old"
    )


def test_backoff_doubles_to_fifteen_minutes_and_success_resets(store: Store) -> None:
    add_pr(store, 1)
    seen = []
    now = NOW
    for _ in range(6):
        res = empty()
        facts(res, listings=0, rate_limited=True, cancelled=True)
        _, finish = run(store, res, now=now)
        out = finish()
        until = out[HOST].paused_until
        assert until is not None
        seen.append((until - now).total_seconds())
        now = until + timedelta(seconds=1)
    assert seen == [60, 120, 240, 480, 900, 900]
    res = empty()
    facts(res)
    _, finish = run(store, res, now=now)
    out = finish()
    assert out[HOST].paused_until is None
    assert row(store)["pause_step"] == 0 and row(store)["paused_until"] is None


def test_concurrent_pause_is_not_shortened_by_a_success(store: Store) -> None:
    add_pr(store, 1)
    store.conn.execute(
        "INSERT INTO hosts (host, paused_until, pause_step) VALUES (?, ?, 240)",
        (HOST, format_ts(NOW + timedelta(minutes=50))),
    )
    store.conn.commit()
    res = empty()
    facts(res)  # a poll that began before the pause and succeeded
    pacing = pc.Pacing(
        NOW, pc.PollMode(), hosts={HOST: pc.HostPlanning(HOST, "github", 6, True, False, None)}
    )
    pc.finish_hosts(store, Config(), res, None, pacing, now=NOW)
    assert row(store)["paused_until"] == format_ts(NOW + timedelta(minutes=50))
    assert row(store)["pause_step"] == 240


def test_rate_limited_five_cycles_never_makes_a_pr_unreachable(store: Store, fake) -> None:
    ids = []
    for n in range(1, 4):
        s = pr(n)
        fake.add(s)
        ids.append(seed(store, s))
    now = NOW
    for _ in range(5):
        fake.rate_limited("snapshot", retry_after=30)
        res = empty()
        pacing = pc.plan_hosts(store.conn, Config(), res, {}, now=now, mode=pc.PollMode())
        chosen = sel.choose_snapshots(store.conn, pacing.plans, now=now, pending_max_age_ms=10**9)
        pool = sn.run_snapshots(
            store,
            make_client(),
            Config(),
            chosen,
            res,
            leases=leases(store),
        )
        pc.finish_hosts(store, Config(), res, pool, pacing, now=now)
        now += timedelta(hours=2)
    rows = store.conn.execute("SELECT status, failure_count FROM prs").fetchall()
    assert {tuple(r) for r in rows} == {("active", 0)}


# --- full pass ---


def test_counter_counts_listed_unrestricted_cycles_and_starts_a_pass(store: Store) -> None:
    add_pr(store, 1, last_polled_at=format_ts(NOW - timedelta(hours=1)))
    cfg = Config.model_validate(dict(poll={"full_every": 3}))
    for i in range(2):
        res = empty()
        facts(res)
        pacing, finish = run(store, res, config=cfg)
        assert pacing.plans[HOST].pass_started_at is None
        finish()
        assert row(store)["poll_count_since_full"] == i + 1
    res = empty()
    facts(res)
    pacing, finish = run(store, res, config=cfg)
    assert pacing.plans[HOST].pass_started_at == NOW
    out = finish()
    r = row(store)
    assert r["poll_count_since_full"] == 0 and r["full_pass_started_at"] == format_ts(NOW)
    assert out[HOST].full_pass and not out[HOST].full_pass_ended


def test_unlisted_and_restricted_cycles_do_not_count(store: Store) -> None:
    add_pr(store, 1)
    _, finish = run(store, empty())  # not listed
    finish()
    assert row(store)["poll_count_since_full"] == 0
    res = empty()
    facts(res)
    pacing, finish = run(store, res, mode=pc.PollMode(restricted=True))
    assert pacing.plans[HOST].pass_started_at is None
    finish()
    r = row(store)
    assert r["poll_count_since_full"] == 0 and r["full_pass_started_at"] is None
    res = empty()
    facts(res)
    _, finish = run(store, res, mode=pc.PollMode(restricted=True, full=True))
    finish()
    assert row(store)["full_pass_started_at"] is None


def test_builtin_and_override_defaults(store: Store) -> None:
    add_pr(store, 1)
    add_pr(store, 1, GL)
    res = empty()
    res.host_list = en.HostList(
        (en.HostEntry(HOST, "github", True), en.HostEntry(GL, "gitlab", True)), frozenset(), True
    )
    pacing = pc.plan_hosts(store.conn, Config(), res, {}, now=NOW, mode=pc.PollMode())
    assert (pacing.plans[HOST].concurrency, pacing.hosts[HOST].full_every) == (4, 6)
    assert (pacing.plans[GL].concurrency, pacing.hosts[GL].full_every) == (2, 12)
    cfg = Config.model_validate(
        dict(hosts={GL: {"full_every": 2, "concurrency": 7}}, poll={"max_snapshots": 5})
    )
    p2 = pc.plan_hosts(store.conn, cfg, res, {}, now=NOW, mode=pc.PollMode())
    assert (p2.plans[GL].concurrency, p2.hosts[GL].full_every, p2.plans[GL].max_snapshots) == (
        7,
        2,
        5,
    )
    assert p2.plans[HOST].concurrency == 4
    chosen = sel.choose_snapshots(store.conn, p2.plans, now=NOW, pending_max_age_ms=10**9)
    assert chosen.concurrency == {HOST: 4, GL: 7}


def real_cycle(
    store: Store, cfg: Config, *, mode: pc.PollMode | None = None
) -> tuple[sel.Selection, sn.PoolResult, dict[str, pc.HostOutcome]]:
    """One cycle through the real planner, selection, pool and `finish_hosts`."""
    now = datetime.now(UTC)
    res = empty()
    facts(res)
    mode = mode or pc.PollMode()
    pacing = pc.plan_hosts(store.conn, cfg, res, {}, now=now, mode=mode)
    chosen = sel.choose_snapshots(
        store.conn, pacing.plans, now=now, pending_max_age_ms=10**9, force=mode.full
    )
    pool = sn.run_snapshots(store, make_client(), cfg, chosen, res, leases=leases(store))
    return chosen, pool, pc.finish_hosts(store, cfg, res, pool, pacing, now=datetime.now(UTC))


def test_full_pass_survives_deferral_and_ends_when_all_done(
    store: Store, fake: FakeForgectl
) -> None:
    for n in range(1, 6):
        baseline(store, fake, pr(n))
    cfg = Config.model_validate(dict(poll={"max_snapshots": 2, "full_every": 1}))
    sizes = []
    ended_at = None
    for cycle_no in range(5):
        chosen, _, out = real_cycle(store, cfg)
        sizes.append(len(chosen.tasks.get(HOST, [])))
        if out[HOST].full_pass_ended:
            ended_at = cycle_no
            break
    assert sizes == [2, 2, 1] and ended_at == 2
    assert row(store)["full_pass_started_at"] is None and row(store)["last_full_at"] is not None


def test_a_counted_failure_is_attempted_once_per_pass_and_the_pass_ends(
    store: Store, fake: FakeForgectl
) -> None:
    ids = [baseline(store, fake, pr(n)) for n in (1, 2)]
    fake.network_error("snapshot")
    cfg = Config.model_validate(dict(poll={"full_every": 1}))
    chosen, pool, out = real_cycle(store, cfg)
    assert len(chosen.tasks[HOST]) == 2 and pool.host(HOST).snapshots == 0
    assert all(col(store, i, "failure_count") == 1 for i in ids)
    assert out[HOST].full_pass_ended
    calls = len(fake.calls("snapshot"))
    chosen2, _, _ = real_cycle(store, cfg)  # the next pass starts, each row once again
    assert len(fake.calls("snapshot")) == calls + len(chosen2.tasks.get(HOST, []))


def test_too_large_snapshot_is_attempted_once_and_the_pass_ends(
    store: Store, fake: FakeForgectl
) -> None:
    pr_id = baseline(store, fake, pr(1))
    fake.oversize("snapshot", extra_bytes=20_000_000)
    cfg = Config.model_validate(dict(poll={"full_every": 1}))
    _, pool, out = real_cycle(store, cfg)
    assert pool.host(HOST).snapshots == 0 and col(store, pr_id, "last_attempt_at") is not None
    assert out[HOST].full_pass_ended


def test_failing_pr_is_tried_once_per_pass(store: Store) -> None:
    add_pr(store, 1, last_polled_at=format_ts(NOW - timedelta(hours=5)), digest="{}")
    add_pr(
        store,
        2,
        last_polled_at=format_ts(NOW - timedelta(hours=5)),
        digest="{}",
        last_attempt_at=format_ts(NOW),
        failure_count=1,
    )
    store.conn.execute(
        "UPDATE prs SET last_polled_at = ? WHERE pr_id LIKE '%-1'", (format_ts(NOW),)
    )
    store.conn.commit()
    res = empty()
    facts(res)
    _, finish = run(store, res, config=Config.model_validate(dict(poll={"full_every": 1})))
    assert finish()[HOST].full_pass_ended


def test_future_pass_start_does_not_stick(store: Store) -> None:
    add_pr(store, 1, last_polled_at=format_ts(NOW))
    store.conn.execute(
        "INSERT INTO hosts (host, full_pass_started_at) VALUES (?, ?)",
        (HOST, format_ts(NOW + timedelta(days=2))),
    )
    store.conn.commit()
    pacing, finish = run(store, empty())
    assert pacing.plans[HOST].pass_started_at == NOW
    assert finish()[HOST].full_pass_ended


def test_full_forces_tasks_for_every_row(store: Store) -> None:
    for n in range(1, 4):
        add_pr(store, n, last_polled_at=format_ts(NOW - timedelta(minutes=1)), digest="{}")
    pacing, _ = run(store, empty(), mode=pc.PollMode(full=True))
    chosen = sel.choose_snapshots(
        store.conn, pacing.plans, now=NOW, pending_max_age_ms=10**9, force=True
    )
    assert len(chosen.tasks[HOST]) == 3 and all(t.forced for t in chosen.tasks[HOST])


def test_forced_pr_task(store: Store) -> None:
    a = add_pr(store, 1)
    m = add_pr(store, 2, status="terminal", terminal_kind="merged")
    c = add_pr(store, 3, status="terminal", terminal_kind="closed")
    assert sel.forced_pr_task(store.conn, a).forced  # type: ignore[union-attr]
    assert sel.forced_pr_task(store.conn, m) is None
    assert sel.forced_pr_task(store.conn, c) is not None
    assert sel.forced_pr_task(store.conn, "nope") is None


# --- budgets and report helpers ---


def test_time_budget_uses_the_monotonic_clock() -> None:
    t = [100.0]
    budget = pc.time_budget(
        Config.model_validate(dict(poll={"max_duration": "10s"})), monotonic=lambda: t[0]
    )
    assert not budget()
    t[0] = 109.9
    assert not budget()
    t[0] = 110.0
    assert budget()


def test_requests_estimate() -> None:
    assert pc.requests_estimate("github", 3) == 21 and pc.requests_estimate("gitlab", 2) == 42


def fin(store: Store, res: en.EnumerationResult, **over: Any) -> bool:
    kw: dict[str, Any] = {"now": NOW, "restricted": False, "exit_ok": True, "interrupted": False}
    kw.update(over)
    return pc.finish_poll(store, res, None, **kw)


def test_finish_poll_writes_only_when_allowed(store: Store) -> None:
    res = empty()
    assert fin(store, res)  # no watches: observed
    store.conn.execute(
        "INSERT INTO watches VALUES ('w1', 'pr', 'k', '2026-01-01T00:00:00.000000Z')"
    )
    store.conn.commit()
    assert not fin(store, res)  # nothing observed
    facts(res)
    assert not fin(store, res, restricted=True)
    assert not fin(store, res, exit_ok=False)
    assert not fin(store, res, interrupted=True)
    assert fin(store, res)


# --- review round 1 ---


def test_a_pr_target_host_with_only_a_closed_row_still_gets_a_plan(store: Store) -> None:
    add_pr(store, 1, status="terminal", terminal_kind="closed")
    res = empty()
    facts(res)
    without, _ = run(store, res, mode=pc.PollMode(restricted=True))
    assert HOST not in without.plans
    with_target, _ = run(store, res, mode=pc.PollMode(restricted=True), pr_hosts=[HOST])
    assert HOST in with_target.plans and with_target.plans[HOST].concurrency == 4


def test_a_paused_host_still_blocks_a_pr_target(store: Store) -> None:
    add_pr(store, 1, status="terminal", terminal_kind="closed")
    store.conn.execute(
        "INSERT INTO hosts (host, paused_until) VALUES (?, ?)",
        (HOST, format_ts(NOW + timedelta(minutes=5))),
    )
    store.conn.commit()
    pacing, _ = run(store, empty(), mode=pc.PollMode(restricted=True), pr_hosts=[HOST])
    assert pacing.plans == {} and HOST in pacing.paused


def test_rate_limited_forced_snapshot_persists_the_pause(store: Store, fake: FakeForgectl) -> None:
    s = pr(1)
    fake.add(s)
    pr_id = seed(store, s, status="terminal", terminal_kind="closed")
    fake.rate_limited("snapshot", retry_after=300)
    cfg = Config()
    res = empty()
    mode = pc.PollMode(restricted=True)
    pacing = pc.plan_hosts(store.conn, cfg, res, {}, now=NOW, mode=mode, pr_hosts=[HOST])
    task = sel.forced_pr_task(store.conn, pr_id)
    assert task is not None
    chosen = sel.Selection(
        tasks={HOST: [task]}, concurrency={h: p.concurrency for h, p in pacing.plans.items()}
    )
    pool = sn.run_snapshots(store, make_client(), cfg, chosen, res, leases=leases(store))
    out = pc.finish_hosts(store, cfg, res, pool, pacing, now=NOW)
    assert out[HOST].paused_until == NOW + timedelta(seconds=300)
    assert row(store)["paused_until"] == format_ts(NOW + timedelta(seconds=300))


def test_finish_hosts_creates_a_planning_for_a_host_seen_only_in_the_pool(store: Store) -> None:
    res = empty()
    pool = sn.PoolResult()
    pool.host(GL).snapshots = 1
    pacing = pc.Pacing(NOW, pc.PollMode(restricted=True))
    out = pc.finish_hosts(store, Config(), res, pool, pacing, now=NOW)
    assert GL in out and row(store, GL)["backend"] is None
    assert row(store, GL)["poll_count_since_full"] == 0


def _hosts_row(store: Store, **cols: Any) -> None:
    names = ", ".join(["host", *cols])
    marks = ", ".join("?" * (len(cols) + 1))
    store.conn.execute(
        f"INSERT INTO hosts ({names}) VALUES ({marks})",  # noqa: S608
        (HOST, *cols.values()),
    )
    store.conn.commit()


def test_a_failed_call_keeps_low_water_and_the_backoff_step(store: Store) -> None:
    add_pr(store, 1)
    low = NOW + timedelta(minutes=30)
    _hosts_row(
        store,
        low_water_until=format_ts(low),
        paused_until=format_ts(NOW - timedelta(minutes=1)),
        pause_step=240,
    )
    pool = sn.PoolResult()
    pool.host(HOST).calls = 1  # a failed call: no snapshot, no conflict
    _, finish = run(store, empty(), pool=pool)
    finish()
    assert row(store)["low_water_until"] == format_ts(low)
    assert row(store)["pause_step"] == 240


def test_a_success_replaces_low_water_and_resets_the_step(store: Store) -> None:
    add_pr(store, 1)
    _hosts_row(
        store,
        low_water_until=format_ts(NOW + timedelta(minutes=30)),
        paused_until=format_ts(NOW - timedelta(minutes=1)),
        pause_step=240,
    )
    pool = sn.PoolResult()
    pool.host(HOST).calls = 1
    pool.host(HOST).snapshots = 1
    _, finish = run(store, empty(), pool=pool)
    finish()
    assert row(store)["low_water_until"] is None
    assert row(store)["pause_step"] == 0


def test_a_rate_limited_listing_does_not_clear_low_water(store: Store) -> None:
    add_pr(store, 1)
    low = NOW + timedelta(minutes=30)
    _hosts_row(store, low_water_until=format_ts(low))
    res = empty()
    facts(res, listings=1, rate_limited=True, cancelled=True)
    _, finish = run(store, res)
    finish()
    assert row(store)["low_water_until"] == format_ts(low)


def test_conflict_discarded_snapshot_is_contact(store: Store) -> None:
    add_pr(store, 1)
    _hosts_row(store, low_water_until=format_ts(NOW + timedelta(minutes=30)))
    pool = sn.PoolResult()
    pool.host(HOST).skipped_conflict = 1
    _, finish = run(store, empty(), pool=pool)
    finish()
    assert row(store)["low_water_until"] is None


def test_time_budget_counts_from_a_given_start() -> None:
    t = [100.0]
    cfg = Config.model_validate(dict(poll={"max_duration": "10s"}))
    budget = pc.time_budget(cfg, started=95.0, monotonic=lambda: t[0])
    assert not budget()
    t[0] = 105.0  # 10s after `started`, 5s after the call
    assert budget()


def test_counter_keeps_counting_while_a_pass_is_open(store: Store) -> None:
    # a row that never finishes the pass keeps it open; the counter is not reset by it
    add_pr(store, 1, last_polled_at=format_ts(NOW - timedelta(hours=9)))
    cfg = Config.model_validate(dict(poll={"full_every": 2}))
    seen = []
    for i in range(4):
        res = empty()
        facts(res)
        _, finish = run(store, res, config=cfg, now=NOW + timedelta(minutes=i))
        finish(NOW + timedelta(minutes=i))
        seen.append(row(store)["poll_count_since_full"])
    assert seen == [1, 0, 1, 2]  # the pass started at the second cycle and stays open
    assert row(store)["full_pass_started_at"] == format_ts(NOW + timedelta(minutes=1))


def test_a_new_pass_starts_at_once_when_the_counter_passed_full_every(store: Store) -> None:
    add_pr(store, 1, last_polled_at=format_ts(NOW))
    _hosts_row(store, poll_count_since_full=5)
    cfg = Config.model_validate(dict(poll={"full_every": 2}))
    res = empty()
    facts(res)
    pacing, _ = run(store, res, config=cfg)
    assert pacing.hosts[HOST].start_pass


def test_forced_task_on_a_merged_row_is_refused_by_the_pool(
    store: Store, fake: FakeForgectl
) -> None:
    s = pr(1)
    fake.add(s)
    pr_id = seed(store, s, status="terminal", terminal_kind="merged")
    task = sel.Task(pr_id, s["pr"], HOST, "pr", forced=True)
    chosen = sel.Selection(tasks={HOST: [task]}, concurrency={HOST: 1})
    res = empty()
    pool = sn.run_snapshots(store, make_client(), Config(), chosen, res, leases=leases(store))
    assert pool.host(HOST).calls == 0 and not fake.calls("snapshot")


def test_closed_row_is_snapshotted_only_by_a_forced_task_and_reopens(
    store: Store, fake: FakeForgectl
) -> None:
    s = pr(1, state="open")
    fake.add(s)
    pr_id = seed(store, s, status="terminal", terminal_kind="closed")
    res = empty()
    task = sel.forced_pr_task(store.conn, pr_id)
    assert task is not None
    plain = dataclasses.replace(task, forced=False)
    chosen = sel.Selection(tasks={HOST: [plain]}, concurrency={HOST: 1})
    sn.run_snapshots(store, make_client(), Config(), chosen, res, leases=leases(store))
    assert not fake.calls("snapshot") and col(store, pr_id, "status") == "terminal"
    chosen = sel.Selection(tasks={HOST: [task]}, concurrency={HOST: 1})
    pool = sn.run_snapshots(store, make_client(), Config(), chosen, res, leases=leases(store))
    assert pool.host(HOST).snapshots == 1 and col(store, pr_id, "status") == "active"


def test_pool_size_is_the_selection_concurrency(
    store: Store, fake: FakeForgectl, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = []

    class Recording(sn.ThreadPoolExecutor):
        def __init__(self, max_workers: int | None = None, **kw: Any) -> None:
            seen.append(max_workers)
            super().__init__(max_workers=max_workers, **kw)

    monkeypatch.setattr(sn, "ThreadPoolExecutor", Recording)
    s = pr(1)
    fake.add(s)
    seed(store, s)
    cfg = Config.model_validate(dict(hosts={HOST: {"concurrency": 7}}))
    pacing = pc.plan_hosts(store.conn, cfg, empty(), {}, now=NOW, mode=pc.PollMode())
    chosen = sel.choose_snapshots(store.conn, pacing.plans, now=NOW, pending_max_age_ms=10**9)
    sn.run_snapshots(store, make_client(), cfg, chosen, empty(), leases=leases(store))
    assert seen == [7]


# --- mutation survivors ---


def test_persisted_pause_is_clamped_and_retry_after_leaves_step(store: Store) -> None:
    add_pr(store, 1)
    _hosts_row(store, pause_step=120)
    res = empty()
    facts(res, listings=0, rate_limited=True, retry_after=86400.0, cancelled=True)
    _, finish = run(store, res)
    finish()
    assert row(store)["paused_until"] == format_ts(NOW + timedelta(hours=1))
    assert row(store)["pause_step"] == 120


def test_pause_expiring_exactly_now_is_not_a_pause(store: Store) -> None:
    _hosts_row(store, paused_until=format_ts(NOW))
    assert pc.paused_hosts(store.conn, NOW) == {}


def test_exhausted_remaining_and_missing_reset(store: Store) -> None:
    add_pr(store, 1)
    res = empty()
    facts(res, lowest_remaining=0, reset_at=format_ts(NOW + timedelta(minutes=20)))
    _, finish = run(store, res)
    finish()
    assert row(store)["low_water_until"] == format_ts(NOW + timedelta(minutes=20))
    res = empty()
    facts(res, lowest_remaining=5)  # low, no reset_at
    _, finish = run(store, res)
    finish()
    assert row(store)["low_water_until"] == format_ts(NOW + timedelta(minutes=15))


@pytest.mark.parametrize(("retry", "expect"), [(600.0, 50), (7200.0, 60)])
def test_later_of_two_pauses_wins(store: Store, retry: float, expect: int) -> None:
    add_pr(store, 1)
    _hosts_row(store, paused_until=format_ts(NOW + timedelta(minutes=50)))
    res = empty()
    facts(res, listings=0, rate_limited=True, retry_after=retry, cancelled=True)
    pacing = pc.Pacing(
        NOW, pc.PollMode(), hosts={HOST: pc.HostPlanning(HOST, "github", 6, False, False, None)}
    )
    pc.finish_hosts(store, Config(), res, None, pacing, now=NOW)
    assert row(store)["paused_until"] == format_ts(NOW + timedelta(minutes=expect))


def test_later_finishing_poll_pause_beyond_an_hour_survives_the_merge(store: Store) -> None:
    add_pr(store, 1)
    other = NOW + timedelta(hours=1, minutes=5)  # written by a poll that finished later
    _hosts_row(store, paused_until=format_ts(other))
    res = empty()
    facts(res, listings=0, rate_limited=True, retry_after=10.0, cancelled=True)
    pacing = pc.Pacing(
        NOW, pc.PollMode(), hosts={HOST: pc.HostPlanning(HOST, "github", 6, False, False, None)}
    )
    pc.finish_hosts(store, Config(), res, None, pacing, now=NOW)
    assert row(store)["paused_until"] == format_ts(other)


def test_unreachable_row_keeps_the_pass_open(store: Store) -> None:
    add_pr(store, 1, last_polled_at=format_ts(NOW))
    add_pr(store, 2, status="unreachable")
    _hosts_row(store, full_pass_started_at=format_ts(NOW - timedelta(minutes=1)))
    res = empty()
    facts(res)
    _, finish = run(store, res)
    out = finish()
    assert not out[HOST].full_pass_ended and row(store)["full_pass_started_at"] is not None


def test_full_starts_a_pass_regardless_of_counter_and_overrides_stored_start(
    store: Store,
) -> None:
    add_pr(store, 1)
    res = empty()
    facts(res)
    mode = pc.PollMode(full=True)
    pacing, finish = run(store, res, mode=mode)  # counter 1 of full_every 6
    assert pacing.hosts[HOST].start_pass and pacing.plans[HOST].pass_started_at == NOW
    finish()
    assert row(store)["full_pass_started_at"] == format_ts(NOW)
    later = NOW + timedelta(minutes=5)  # a pass started earlier: `--full` still hands "now"
    pacing, _ = run(store, empty(), now=later, mode=mode)
    assert pacing.plans[HOST].pass_started_at == later


def test_query_only_listing_counts(store: Store) -> None:
    add_pr(store, 1)
    res = empty()
    facts(res, listings=0, queried=True)
    _, finish = run(store, res)
    finish()
    assert row(store)["poll_count_since_full"] == 1


def test_unknown_backend_defaults(store: Store) -> None:
    add_pr(store, 1, GL)
    add_pr(store, 1)
    pacing = pc.plan_hosts(store.conn, Config(), empty(), {}, now=NOW, mode=pc.PollMode())
    assert (pacing.hosts[GL].backend, pacing.plans[GL].concurrency) == ("gitlab", 2)
    assert (pacing.hosts[HOST].backend, pacing.plans[HOST].concurrency) == ("github", 4)


def test_conflict_discarded_snapshot_counts_as_observed(store: Store) -> None:
    store.conn.execute(
        "INSERT INTO watches VALUES ('w1', 'pr', 'k', '2026-01-01T00:00:00.000000Z')"
    )
    store.conn.commit()
    pool = sn.PoolResult()
    pool.host(HOST).skipped_conflict = 1
    assert pc.finish_poll(
        store, empty(), pool, now=NOW, restricted=False, exit_ok=True, interrupted=False
    )


@pytest.mark.parametrize("why", ["rate_limited", "cancelled", "skipped", "failed"])
def test_blocked_hosts_get_no_plan(store: Store, why: str) -> None:
    add_pr(store, 1)
    res = empty()
    f = facts(res)
    if why == "skipped":
        f.skipped = "no_credential"
    elif why == "failed":
        res.host_list = en.HostList((), frozenset({HOST}), True)
    else:
        setattr(f, why, True)
    pacing = pc.plan_hosts(store.conn, Config(), res, {}, now=NOW, mode=pc.PollMode())
    assert HOST not in pacing.plans and HOST in pacing.hosts


# --- per host persistence (#25) ---


def test_finish_host_writes_one_host_and_finish_hosts_does_not_write_it_again(
    store: Store,
) -> None:
    add_pr(store, 1)
    add_pr(store, 2, GL)
    res = empty()
    facts(res, listings=1)
    facts(res, GL, listings=1)
    pacing, _ = run(store, res)
    out = pc.finish_host(store, Config(), res, None, pacing, HOST, now=NOW)
    assert row(store)["poll_count_since_full"] == 1 and row(store, GL) == {}
    assert out.host == HOST
    done = pc.finish_hosts(store, Config(), res, None, pacing, now=NOW)
    assert row(store)["poll_count_since_full"] == 1  # not counted twice
    assert row(store, GL)["poll_count_since_full"] == 1
    assert set(done) == {HOST, GL}


def test_a_rate_limit_is_persisted_when_the_host_is_finished(store: Store) -> None:
    add_pr(store, 1)
    res = empty()
    facts(res, rate_limited=True, retry_after=120.0, cancelled=True)
    pacing, _ = run(store, res)
    pc.finish_host(store, Config(), res, None, pacing, HOST, now=NOW)
    assert row(store)["paused_until"] == format_ts(NOW + timedelta(seconds=120))


def test_pause_hosts_pauses_each_host_clamped_and_keeps_a_later_pause(store: Store) -> None:
    store.conn.execute(
        "INSERT INTO hosts (host, paused_until) VALUES (?, ?)",
        (GL, format_ts(NOW + timedelta(minutes=50))),
    )
    store.conn.commit()
    until = pc.pause_hosts(store, [HOST, GL], 99999.0, now=NOW)
    assert until[HOST] == NOW + timedelta(hours=1)  # clamped like any pause
    assert until[GL] == NOW + timedelta(hours=1)
    assert row(store)["paused_until"] == format_ts(NOW + timedelta(hours=1))
    assert pc.paused_hosts(store.conn, NOW).keys() == {HOST, GL}  # the next process honours it


def test_pause_hosts_without_retry_after_backs_off_and_doubles(store: Store) -> None:
    pc.pause_hosts(store, [HOST], None, now=NOW)
    assert row(store)["paused_until"] == format_ts(NOW + timedelta(seconds=60))
    assert row(store)["pause_step"] == 60
    later = NOW + timedelta(minutes=2)
    pc.pause_hosts(store, [HOST], None, now=later)
    assert row(store)["paused_until"] == format_ts(later + timedelta(seconds=120))
