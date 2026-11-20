"""Snapshot selection: triggers (a) to (f), order and the per-host cap (#22)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pr_watch import selection as sel
from pr_watch.state_dir import open_state_dir
from pr_watch.store import Store, format_ts, open_store

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
HOST = "github.com"
HOUR = 3600 * 1000
OPEN = {
    "state": "open",
    "head_sha": "a" * 40,
    "blocked_by": [],
    "ci": {"state": "success", "hold": 0},
}
PENDING = {**OPEN, "ci": {"state": "pending", "hold": 0}}


def ts(delta: timedelta = timedelta(0)) -> str:
    return format_ts(NOW + delta)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with open_state_dir(tmp_path / "pr-watch") as s, open_store(s) as st:
        yield st


_n = 0


def add(store: Store, name: str = "", host: str = HOST, **cols: Any) -> str:
    """A row with a digest, polled one hour ago and in sync, unless `cols` says otherwise."""
    global _n
    _n += 1
    pr_id = name or f"PR_{_n:05d}"
    base: dict[str, Any] = {
        "pr": f"{host}/example-org/widgets#{_n}",
        "repo": f"{host}/example-org/widgets",
        "host": host,
        "digest": json.dumps(OPEN),
        "snap_updated_at": ts(-timedelta(hours=2)),
        "listed_updated_at": ts(-timedelta(hours=2)),
        "last_polled_at": ts(-timedelta(hours=1)),
    }
    base.update(cols)
    names = ["pr_id", *base]
    store.conn.execute(
        f"INSERT INTO prs ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",  # noqa: S608
        (pr_id, *base.values()),
    )
    wid = f"w-{_n:010d}"
    store.conn.execute(
        "INSERT INTO watches VALUES (?, 'pr', ?, '2026-01-01T00:00:00.000000Z')", (wid, pr_id)
    )
    store.conn.execute("INSERT INTO sources (pr_id, watch_id) VALUES (?, ?)", (pr_id, wid))
    store.conn.commit()
    return pr_id


def choose(store: Store, **kw: Any) -> sel.Selection:
    plan = kw.pop("plan", None) or sel.HostPlan(HOST, concurrency=4)
    plans = plan if isinstance(plan, dict) else {plan.host: plan}
    return sel.choose_snapshots(
        store.conn, plans, now=NOW, pending_max_age_ms=kw.pop("age", 6 * HOUR), **kw
    )


def ids(s: sel.Selection, host: str = HOST) -> list[str]:
    return [t.pr_id for t in s.tasks.get(host, [])]


def groups(s: sel.Selection, host: str = HOST) -> list[str]:
    return [t.group for t in s.tasks.get(host, [])]


def test_a_settled_in_sync_row_is_not_chosen(store: Store) -> None:
    add(store)
    assert ids(choose(store)) == []


def test_a_no_digest_and_rebaseline(store: Store) -> None:
    a = add(store, digest=None)
    b = add(store, rebaseline=1)
    s = choose(store)
    assert sorted(ids(s)) == sorted([a, b]) and set(groups(s)) == {"a"}


def test_b_listed_newer_than_snapshot(store: Store) -> None:
    b = add(store, listed_updated_at=ts(-timedelta(hours=1)))
    assert ids(choose(store)) == [b]


def test_b_equal_listed_and_snapshot_is_not_a_trigger(store: Store) -> None:
    add(store, listed_updated_at=ts(-timedelta(hours=2)))
    assert ids(choose(store)) == []


def test_b_snapshot_older_than_listing_but_committed_is_not_chosen_again(store: Store) -> None:
    # snap_updated_at is the larger of the two, so the V4 case settles after one snapshot
    add(
        store,
        listed_updated_at=ts(-timedelta(minutes=5)),
        snap_updated_at=ts(-timedelta(minutes=5)),
    )
    assert ids(choose(store)) == []


def test_b_null_snap_updated_at_with_a_listing_is_chosen(store: Store) -> None:
    b = add(store, snap_updated_at=None, listed_updated_at=ts(-timedelta(hours=1)))
    assert ids(choose(store)) == [b]


def test_b_a_listing_one_microsecond_newer_is_chosen(store: Store) -> None:
    # the incomplete-listing mark: snap_updated_at plus one microsecond
    snap = NOW - timedelta(hours=2)
    b = add(store, snap_updated_at=format_ts(snap),
            listed_updated_at=format_ts(snap + timedelta(microseconds=1)))  # fmt: skip
    assert ids(choose(store)) == [b]


def test_c_unsettled_not_stuck_is_chosen_every_cycle(store: Store) -> None:
    c = add(store, digest=json.dumps(PENDING), unsettled_since=ts(-timedelta(hours=1)))
    s = choose(store)
    assert ids(s) == [c] and groups(s) == ["c"]


def test_c_unsettled_with_no_start_is_chosen(store: Store) -> None:
    # a baseline taken unsettled by `watch add` (#18) has no start yet
    c = add(store, digest=json.dumps(PENDING), unsettled_since=None)
    assert ids(choose(store)) == [c]


def test_c_stuck_is_not_chosen_outside_a_pass(store: Store) -> None:
    add(store, digest=json.dumps(PENDING), unsettled_since=ts(-timedelta(hours=7)))
    assert ids(choose(store)) == []


def test_c_stuck_boundary_is_at_least_the_age(store: Store) -> None:
    add(store, digest=json.dumps(PENDING), unsettled_since=ts(-timedelta(hours=6)))
    young = add(store, digest=json.dumps(PENDING), unsettled_since=ts(-timedelta(hours=5)))
    assert ids(choose(store)) == [young]


@pytest.mark.parametrize(
    "digest",
    [
        {**OPEN, "ci": {"state": "success", "hold": 2}},
        {**OPEN, "blocked_by": ["mergeability_unknown"]},
    ],
)
def test_c_hold_and_mergeability_unknown_are_unsettled(
    store: Store, digest: dict[str, Any]
) -> None:
    c = add(store, digest=json.dumps(digest), unsettled_since=ts(-timedelta(hours=1)))
    assert ids(choose(store)) == [c]


def test_c_a_digest_that_merely_mentions_pending_is_not_unsettled(store: Store) -> None:
    add(store, digest=json.dumps({**OPEN, "title": "pending hold mergeability_unknown"}))
    assert ids(choose(store)) == []


def test_d_a_source_that_left_twice(store: Store) -> None:
    d = add(store)
    store.conn.execute("UPDATE sources SET left_count = 1")
    assert ids(choose(store)) == []
    store.conn.execute("UPDATE sources SET left_count = 2")
    s = choose(store)
    assert ids(s) == [d] and groups(s) == ["d"]


def test_terminal_and_unreachable_rows_are_not_chosen_by_a_to_d(store: Store) -> None:
    add(store, status="terminal", terminal_kind="closed", terminal_at=ts(), digest=None,
        rebaseline=1, listed_updated_at=ts())  # fmt: skip
    add(store, status="unreachable", digest=None, rebaseline=1, listed_updated_at=ts())
    assert ids(choose(store)) == []


def test_order_is_b_d_a_c_then_oldest_polled_first(store: Store) -> None:
    c = add(store, digest=json.dumps(PENDING), unsettled_since=ts(-timedelta(hours=1)))
    a = add(store, digest=None, last_polled_at=None)
    d = add(store)
    store.conn.execute("UPDATE sources SET left_count = 2 WHERE pr_id = ?", (d,))
    b_new = add(store, listed_updated_at=ts(), last_polled_at=ts(-timedelta(minutes=1)))
    b_old = add(store, listed_updated_at=ts(), last_polled_at=ts(-timedelta(hours=3)))
    s = choose(store)
    assert ids(s) == [b_old, b_new, d, a, c]
    assert groups(s) == ["b", "b", "d", "a", "c"]


def test_a_row_in_several_groups_is_chosen_once_in_the_first(store: Store) -> None:
    x = add(store, digest=None, listed_updated_at=ts())
    s = choose(store)
    assert ids(s) == [x] and groups(s) == ["b"]


def test_only_planned_hosts_are_chosen(store: Store) -> None:
    add(store, digest=None, host="gitlab.example.net")
    mine = add(store, digest=None)
    s = choose(store)
    assert ids(s) == [mine] and "gitlab.example.net" not in s.tasks


def test_only_pr_ids_restricts(store: Store) -> None:
    add(store, digest=None)
    keep = add(store, digest=None)
    assert ids(choose(store, only_pr_ids={keep})) == [keep]


# --- full pass ---


def passing(started: timedelta = -timedelta(minutes=10), **kw: Any) -> sel.HostPlan:
    return sel.HostPlan(HOST, concurrency=4, pass_started_at=NOW + started, **kw)


def test_e_pass_in_progress_chooses_rows_not_done_for_the_pass(store: Store) -> None:
    todo = add(store, last_polled_at=ts(-timedelta(hours=1)))
    add(store, last_polled_at=ts(-timedelta(minutes=5)))  # polled since the pass started
    failed = add(
        store, last_polled_at=ts(-timedelta(hours=1)), last_attempt_at=ts(-timedelta(minutes=1))
    )
    s = choose(store, plan=passing())
    assert ids(s) == [todo] and groups(s) == ["e"]
    assert failed not in ids(s)  # a failed attempt counts as done for the pass


def test_e_pass_includes_stuck_rows(store: Store) -> None:
    stuck = add(store, digest=json.dumps(PENDING), unsettled_since=ts(-timedelta(hours=9)))
    s = choose(store, plan=passing())
    assert ids(s) == [stuck] and groups(s) == ["e"]


def test_f_unreachable_only_on_a_pass(store: Store) -> None:
    u = add(store, status="unreachable", last_polled_at=None)
    assert ids(choose(store)) == []
    s = choose(store, plan=passing())
    assert ids(s) == [u] and groups(s) == ["f"]


def test_e_precedes_f_and_terminal_is_never_chosen(store: Store) -> None:
    u = add(store, status="unreachable", last_polled_at=None)
    e = add(store)
    add(store, status="terminal", terminal_kind="merged", terminal_at=ts(-timedelta(hours=1)))
    assert ids(choose(store, plan=passing())) == [e, u]


def test_low_water_limits_to_a_to_d(store: Store) -> None:
    a = add(store, digest=None)
    add(store)
    add(store, status="unreachable")
    assert ids(choose(store, plan=passing(limited=True))) == [a]


def test_no_pass_in_progress_chooses_no_e_or_f(store: Store) -> None:
    add(store)
    assert ids(choose(store)) == []


# --- cap ---


def test_cap_defers_the_rest_in_order(store: Store) -> None:
    rows = [
        add(store, digest=None, last_polled_at=ts(-timedelta(minutes=60 - i))) for i in range(10)
    ]
    s = choose(store, plan=sel.HostPlan(HOST, concurrency=4, max_snapshots=4))
    assert ids(s) == rows[:4] and s.deferred == {HOST: 6}


def test_cap_zero_is_unlimited(store: Store) -> None:
    for _ in range(60):
        add(store, digest=None)
    s = choose(store, plan=sel.HostPlan(HOST, concurrency=4, max_snapshots=0))
    assert len(ids(s)) == 60 and s.deferred == {}


def test_100_unsettled_members_get_40_a_cycle_and_none_is_starved(store: Store) -> None:
    every = [
        add(store, digest=json.dumps(PENDING), unsettled_since=ts(-timedelta(hours=1)),
            last_polled_at=ts(-timedelta(minutes=200 - i)))
        for i in range(100)
    ]  # fmt: skip
    seen: list[str] = []
    for _ in range(3):
        s = choose(store, plan=sel.HostPlan(HOST, concurrency=4, max_snapshots=40))
        got = ids(s)
        assert len(got) == 40 and s.deferred == {HOST: 60}
        seen.extend(got)
        for pr_id in got:  # the commit moves last_polled_at to now
            store.conn.execute("UPDATE prs SET last_polled_at = ? WHERE pr_id = ?", (ts(), pr_id))
        store.conn.commit()
    assert len(set(seen[:100])) == 100 and set(seen) == set(
        every
    )  # a round robin, no repeats early


def test_c_takes_at_most_three_quarters_while_a_pass_waits(store: Store) -> None:
    for i in range(10):
        add(store, digest=json.dumps(PENDING), unsettled_since=ts(-timedelta(hours=1)),
            last_polled_at=ts(-timedelta(minutes=100 - i)))  # fmt: skip
    waiting = [add(store, last_polled_at=ts(-timedelta(hours=1))) for _ in range(5)]
    s = choose(store, plan=passing(max_snapshots=8))
    assert groups(s).count("c") == 6 and groups(s).count("e") == 2
    assert set(ids(s)[6:]) <= set(waiting)
    assert s.deferred == {HOST: 15 - 8}


def test_c_takes_the_whole_cap_when_no_pass_row_waits(store: Store) -> None:
    for _ in range(10):
        add(store, digest=json.dumps(PENDING), unsettled_since=ts(-timedelta(hours=1)))
    s = choose(store, plan=passing(max_snapshots=8))
    assert groups(s).count("c") == 8


def test_b_d_a_are_not_limited_by_the_quarter(store: Store) -> None:
    for _ in range(8):
        add(store, digest=None)
    add(store)  # a row waiting for the pass
    s = choose(store, plan=passing(max_snapshots=8))
    assert groups(s) == ["a"] * 8 and s.deferred == {HOST: 1}


def test_caps_are_per_host(store: Store) -> None:
    for _ in range(5):
        add(store, digest=None)
        add(store, digest=None, host="gitlab.example.net")
    plans = {
        HOST: sel.HostPlan(HOST, concurrency=4, max_snapshots=2),
        "gitlab.example.net": sel.HostPlan("gitlab.example.net", concurrency=2, max_snapshots=3),
    }
    s = choose(store, plan=plans)
    assert (len(s.tasks[HOST]), len(s.tasks["gitlab.example.net"])) == (2, 3)
    assert s.deferred == {HOST: 3, "gitlab.example.net": 2}


def test_selection_reads_persisted_triggers_after_a_reopen(store: Store) -> None:
    # a reactivated row: status active, terminal_at cleared, rebaseline set
    r = add(store, rebaseline=1, terminal_at=None, terminal_kind=None)
    assert ids(choose(store)) == [r]
