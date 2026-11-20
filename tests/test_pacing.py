"""Pacing: host pause, low water, full pass, budgets (#23)."""

from __future__ import annotations

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
from tests.test_snapshots import HOST, cycle, fake, make_client, pr, seed  # noqa: F401

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


def run(store, res, *, now=NOW, mode=None, pool=None, config=None):
    cfg = config or Config()
    mode = mode or pc.PollMode()
    paused = pc.paused_hosts(store.conn, now)
    pacing = pc.plan_hosts(store.conn, cfg, res, paused, now=now, mode=mode)
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
    facts(res, GL, lowest_remaining=None)  # GitLab without headers
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


def test_stored_limit_is_clamped_to_an_hour(store: Store) -> None:
    add_pr(store, 1)
    store.conn.execute(
        "INSERT INTO hosts (host, low_water_until, paused_until) VALUES (?, ?, ?)",
        (HOST, format_ts(NOW + timedelta(days=3)), format_ts(NOW + timedelta(days=3))),
    )
    store.conn.commit()
    assert pc.paused_hosts(store.conn, NOW)[HOST] == NOW + timedelta(hours=1)
    assert (
        HOST
        not in pc.plan_hosts(
            store.conn,
            Config(),
            empty(),
            {},
            now=NOW + timedelta(hours=1, seconds=1),
            mode=pc.PollMode(),
        ).paused
    )  # noqa: E501


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


def test_rate_limited_five_cycles_never_makes_a_pr_unreachable(store: Store, fake) -> None:  # noqa: F811
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
            leases=__import__("tests.test_snapshots", fromlist=["x"]).leases(store),
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


def test_full_pass_survives_deferral_and_ends_when_all_done(store: Store) -> None:
    ids = [
        add_pr(store, n, last_polled_at=format_ts(NOW - timedelta(hours=5)), digest="{}")
        for n in range(1, 8)
    ]
    cfg = Config.model_validate(dict(poll={"max_snapshots": 3, "full_every": 1}))
    now = NOW
    ended_at = None
    for cycle_no in range(5):
        res = empty()
        facts(res)
        pacing, finish = run(store, res, now=now, config=cfg)
        chosen = sel.choose_snapshots(store.conn, pacing.plans, now=now, pending_max_age_ms=10**9)
        for t in chosen.tasks.get(HOST, []):  # the snapshot succeeded
            store.conn.execute(
                "UPDATE prs SET last_polled_at = ? WHERE pr_id = ?", (format_ts(now), t.pr_id)
            )
        store.conn.commit()
        out = finish(now)
        if out[HOST].full_pass_ended:
            ended_at = cycle_no
            break
        now += timedelta(minutes=5)
    assert ended_at == 2  # 7 rows at 3 per cycle
    assert row(store)["full_pass_started_at"] is None and row(store)["last_full_at"] is not None
    assert ids


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
