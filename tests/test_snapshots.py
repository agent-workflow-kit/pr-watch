"""The snapshot pool and the per-PR commit pipeline against the fake forgectl (#22)."""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pr_watch import enumeration as en
from pr_watch import log as log_module
from pr_watch import selection as sel
from pr_watch import snapshots as sn
from pr_watch.config import Config
from pr_watch.diff import Drop
from pr_watch.forgectl_client import ForgectlClient, Outcome, resolve_executable
from pr_watch.leases import Leases, ProcFs
from pr_watch.log import Log
from pr_watch.refs import PrRef
from pr_watch.state_dir import open_state_dir
from pr_watch.store import Store, format_ts, open_store
from pr_watch.watches import new_watch_id
from tests.forgectl_support import FakeForgectl, new_pr, sha
from tests.harness import assert_valid

NOW = datetime.now(UTC)
HOST = "github.com"
TTL = timedelta(minutes=6)
CREATED = "2026-01-01T00:00:00.000000Z"


class Rec(Log):
    """A log that records what the pool writes."""

    def __init__(self) -> None:
        super().__init__(None)
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def write(self, event: str, level: str = "info", **fields: Any) -> bool:
        # every record the pool writes must be one schemas/log.json accepts
        line = log_module._build(event, level, fields, time.time())
        assert_valid("log", json.loads(line))
        self.records.append((event, level, fields))
        return True

    def of(self, event: str) -> list[dict[str, Any]]:
        return [f for e, _, f in self.records if e == event]


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with open_state_dir(tmp_path / "pr-watch") as s, open_store(s) as st:
        yield st


@pytest.fixture
def fake(fake_forgectl: FakeForgectl) -> FakeForgectl:
    return fake_forgectl.host(HOST, me="user-1")


def make_client() -> ForgectlClient:
    exe = resolve_executable(os.environ["PATH"])
    assert exe is not None
    return ForgectlClient(
        exe, timeout_ms=5000, max_output=4 * 1024 * 1024, env=dict(os.environ),
        deadline_slack=5.0, term_grace=0.3,
    )  # fmt: skip


def pr(n: int, **ch: Any) -> dict[str, Any]:
    ch.setdefault("updated_at", "2026-10-02T10:30:00Z")
    return new_pr(HOST, "example-org/widgets", n, **ch)


def seed(store: Store, snap: dict[str, Any], **cols: Any) -> str:
    c = store.conn
    pr_id = snap["pr_id"]
    c.execute(
        "INSERT INTO prs (pr_id, pr, repo, host) VALUES (?, ?, ?, ?)",
        (pr_id, snap["pr"], snap["repo"], snap["pr"].partition("/")[0]),
    )
    for k, v in cols.items():
        c.execute(f"UPDATE prs SET {k} = ? WHERE pr_id = ?", (v, pr_id))  # noqa: S608
    wid = new_watch_id()
    c.execute(
        "INSERT INTO watches VALUES (?, 'pr', ?, '2026-01-01T00:00:00.000000Z')", (wid, pr_id)
    )
    c.execute("INSERT INTO owners VALUES (?, 'me')", (wid,))
    c.execute("INSERT INTO sources (pr_id, watch_id) VALUES (?, ?)", (pr_id, wid))
    c.commit()
    return str(pr_id)


def leases(store: Store) -> Leases:
    return Leases(store, TTL)


def cycle(
    store: Store,
    config: Config | None = None,
    *,
    client: Any = None,
    plan: sel.HostPlan | None = None,
    log: Rec | None = None,
    res: en.EnumerationResult | None = None,
    lease: Leases | None = None,
    **kw: Any,
) -> tuple[sn.PoolResult, en.EnumerationResult]:
    cfg = config or Config()
    plan = plan or sel.HostPlan(HOST, concurrency=4)
    chosen = sel.choose_snapshots(
        store.conn, {plan.host: plan}, now=datetime.now(UTC), pending_max_age_ms=6 * 3600 * 1000
    )
    res = res or en.EnumerationResult(en.HostList((), frozenset(), True))
    out = sn.run_snapshots(
        store, client or make_client(), cfg, chosen, res,
        concurrency={plan.host: plan.concurrency}, leases=lease or leases(store),
        log=log, **kw,
    )  # fmt: skip
    return out, res


def col(store: Store, pr_id: str, name: str) -> Any:
    r = store.conn.execute(f"SELECT {name} FROM prs WHERE pr_id = ?", (pr_id,)).fetchone()  # noqa: S608
    return None if r is None else r[0]


def event_types(store: Store) -> list[str]:
    return [r[0] for r in store.conn.execute("SELECT type FROM events ORDER BY seq")]


def lease_count(store: Store) -> int:
    return int(store.conn.execute("SELECT count(*) FROM leases").fetchone()[0])


def baseline(store: Store, fake: FakeForgectl, snap: dict[str, Any], **cols: Any) -> str:
    fake.add(snap)
    pr_id = seed(store, snap, **cols)
    out, _ = cycle(store)
    assert out.host(HOST).snapshots == 1
    return pr_id


# --- commit pipeline ---


def test_first_snapshot_baselines_silently_and_sets_the_commit_columns(
    store: Store, fake: FakeForgectl
) -> None:
    pr_id = baseline(store, fake, pr(1))
    assert col(store, pr_id, "digest") is not None and col(store, pr_id, "rev") == 1
    assert col(store, pr_id, "last_polled_at") is not None
    assert col(store, pr_id, "snap_updated_at") == "2026-10-02T10:30:00.000000Z"
    assert col(store, pr_id, "baselined_at") is not None
    assert event_types(store) == [] and lease_count(store) == 0


def test_a_change_commits_events_and_the_digest_together(store: Store, fake: FakeForgectl) -> None:
    snap = pr(1)
    pr_id = baseline(store, fake, snap)
    fake.update(snap["pr"], head={"sha": sha(5)}, updated_at="2026-10-02T11:00:00Z")
    store.conn.execute("UPDATE prs SET listed_updated_at = '2026-10-02T11:00:00.000000Z'")
    store.conn.commit()
    out, _ = cycle(store)
    assert event_types(store) == ["pushed"] and out.host(HOST).events == 1
    assert col(store, pr_id, "rev") == 2 and len(out.events) == 1
    stored = json.loads(col(store, pr_id, "digest"))
    assert stored["head_sha"] == sha(5)
    ev = store.conn.execute("SELECT payload FROM events").fetchone()[0]
    assert json.loads(ev)["watches"]  # read inside the commit transaction


def test_unchanged_row_is_not_snapshotted_again(store: Store, fake: FakeForgectl) -> None:
    baseline(store, fake, pr(1))
    before = len(fake.calls("snapshot"))
    out, _ = cycle(store)
    assert out.needed == 0 and len(fake.calls("snapshot")) == before


def test_snapshot_older_than_its_listing_is_not_chosen_again(
    store: Store, fake: FakeForgectl
) -> None:
    # V4: snapshot.updated_at is not assumed to be at least the listed value
    snap = pr(1, updated_at="2026-10-02T10:00:00Z")
    fake.add(snap)
    pr_id = seed(store, snap, listed_updated_at="2026-10-02T10:45:00.000000Z")
    cycle(store)
    assert col(store, pr_id, "snap_updated_at") == "2026-10-02T10:45:00.000000Z"
    out, _ = cycle(store)
    assert out.needed == 0


def test_a_snapshot_with_another_pr_id_stores_nothing(store: Store, fake: FakeForgectl) -> None:
    snap = pr(1)
    fake.add(snap)
    seed(store, {**snap, "pr_id": "PR_old"})
    log = Rec()
    out, res = cycle(store, log=log)
    assert col(store, "PR_old", "digest") is None and col(store, "PR_old", "rev") == 0
    assert col(store, "PR_old", "failure_count") == 1
    assert col(store, "PR_old", "last_failure_code") == "pr_id_changed"
    assert col(store, "PR_old", "pr") == snap["pr"]  # not refreshed
    assert event_types(store) == [] and out.host(HOST).snapshots == 0
    assert [f.code for f in res.failures] == ["pr_id_changed"]
    assert log.of("snapshot_failed")[0]["code"] == "pr_id_changed"


def test_a_closed_snapshot_older_than_a_newer_listing_is_not_committed(
    store: Store, fake: FakeForgectl
) -> None:
    snap = pr(
        1, state="closed", closed_at="2026-10-02T10:00:00Z", updated_at="2026-10-02T10:00:00Z"
    )
    fake.add(snap)
    pr_id = seed(store, snap, listed_updated_at="2026-10-02T10:30:00.000000Z", digest="{}")
    log = Rec()
    out, _ = cycle(store, log=log)
    assert col(store, pr_id, "status") == "active" and col(store, pr_id, "rev") == 0
    assert col(store, pr_id, "failure_count") == 0 and event_types(store) == []
    assert out.host(HOST).skipped_conflict == 0
    assert log.of("commit_skipped")[0]["reason"] == "newer_listing"
    # its persisted trigger still selects it
    again = sel.choose_snapshots(
        store.conn, {HOST: sel.HostPlan(HOST, 4)}, now=NOW, pending_max_age_ms=1000
    )
    assert [t.pr_id for t in again.tasks[HOST]] == [pr_id]


def test_membership_rule_drops_the_row_when_the_left_query_was_its_last_source(
    store: Store, fake: FakeForgectl
) -> None:
    snap = pr(1)
    fake.add(snap)
    pr_id = seed(store, snap)
    store.conn.execute(
        "INSERT INTO watches VALUES ('w-qqqqqqqqqq', 'query', 'mine+open', ?)", (CREATED,)
    )
    store.conn.execute("INSERT INTO sources VALUES (?, 'w-qqqqqqqqqq', 2)", (pr_id,))
    store.conn.execute("DELETE FROM sources WHERE watch_id != 'w-qqqqqqqqqq'")  # the only source
    store.conn.commit()
    out, _ = cycle(store)
    assert out.host(HOST).snapshots == 1
    assert col(store, pr_id, "pr") is None  # last source gone: the row is dropped


def test_events_of_a_commit_that_drops_the_last_source_keep_their_watches_and_labels(
    store: Store, fake: FakeForgectl
) -> None:
    snap = pr(1)
    pr_id = baseline(store, fake, snap)
    store.conn.execute(
        "INSERT INTO watches VALUES ('w-qqqqqqqqqq', 'query', 'mine+open', ?)", (CREATED,)
    )
    store.conn.execute("INSERT INTO owners VALUES ('w-qqqqqqqqqq', 'me')")
    store.conn.execute("INSERT INTO labels VALUES ('w-qqqqqqqqqq', 'me', 'team=core')")
    store.conn.execute("INSERT INTO sources VALUES (?, 'w-qqqqqqqqqq', 2)", (pr_id,))
    store.conn.execute("DELETE FROM sources WHERE watch_id != 'w-qqqqqqqqqq'")  # query-only now
    fake.update(snap["pr"], head={"sha": sha(5)}, updated_at="2026-10-02T11:00:00Z")
    store.conn.execute("UPDATE prs SET listed_updated_at = '2026-10-02T11:00:00.000000Z'")
    store.conn.commit()
    out, _ = cycle(store)
    assert out.host(HOST).events == 1 and col(store, pr_id, "pr") is None  # row dropped
    labels, payload = store.conn.execute("SELECT labels, payload FROM events").fetchone()
    assert json.loads(payload)["watches"] == ["w-qqqqqqqqqq"]
    assert json.loads(labels) == ["team=core"]


def test_membership_rule_removes_only_the_left_query_source(
    store: Store, fake: FakeForgectl
) -> None:
    snap = pr(1)
    fake.add(snap)
    pr_id = seed(store, snap)
    store.conn.execute(
        "INSERT INTO watches VALUES ('w-qqqqqqqqqq', 'query', 'mine+open', ?)", (CREATED,)
    )
    store.conn.execute("INSERT INTO sources VALUES (?, 'w-qqqqqqqqqq', 2)", (pr_id,))
    store.conn.commit()
    cycle(store)
    kinds = [r[0] for r in store.conn.execute(
        "SELECT w.kind FROM sources s JOIN watches w ON w.id = s.watch_id"
    )]  # fmt: skip
    assert kinds == ["pr"] and col(store, pr_id, "digest") is not None


def test_membership_rule_keeps_the_source_of_a_closed_snapshot(
    store: Store, fake: FakeForgectl
) -> None:
    snap = pr(
        1, state="closed", closed_at="2026-10-02T10:00:00Z", updated_at="2026-10-02T10:00:00Z"
    )
    fake.add(snap)
    pr_id = seed(store, snap)
    store.conn.execute("UPDATE sources SET left_count = 0")
    store.conn.execute(
        "INSERT INTO watches VALUES ('w-qqqqqqqqqq', 'query', 'mine+open', ?)", (CREATED,)
    )
    store.conn.execute("INSERT INTO sources VALUES (?, 'w-qqqqqqqqqq', 2)", (pr_id,))
    store.conn.commit()
    cycle(store)
    assert event_types(store) == ["closed"]
    assert store.conn.execute("SELECT count(*) FROM sources").fetchone()[0] == 2


def test_a_label_union_over_the_cap_is_cut_sorted_and_logged(
    store: Store, fake: FakeForgectl
) -> None:
    snap = pr(1)
    pr_id = baseline(store, fake, snap)
    for w in range(5):  # five watches of 64 labels each: 320 distinct labels
        wid = new_watch_id()
        store.conn.execute(
            "INSERT INTO watches VALUES (?, 'pr', ?, '2026-01-01T00:00:00.000000Z')", (wid, f"k{w}")
        )
        store.conn.execute("INSERT INTO owners VALUES (?, 'o')", (wid,))
        store.conn.execute("INSERT INTO sources (pr_id, watch_id) VALUES (?, ?)", (pr_id, wid))
        for i in range(64):
            store.conn.execute("INSERT INTO labels VALUES (?, 'o', ?)", (wid, f"team=l{w}-{i:03d}"))
    fake.update(snap["pr"], head={"sha": sha(6)}, updated_at="2026-10-02T11:00:00Z")
    store.conn.execute("UPDATE prs SET listed_updated_at = '2026-10-02T11:00:00.000000Z'")
    store.conn.commit()
    log = Rec()
    cycle(store, log=log)
    labels = json.loads(store.conn.execute("SELECT labels FROM events").fetchone()[0])
    assert len(labels) == 256 and labels == sorted(labels)
    (capped,) = log.of("labels_capped")
    assert capped == {"pr": snap["pr"], "pr_id": pr_id, "kept": 256, "total": 320}
    assert log.of("commit_skipped") == []


def test_a_label_union_over_the_cap_is_not_logged_when_the_snapshot_has_no_event(
    store: Store, fake: FakeForgectl
) -> None:
    snap = pr(1)
    pr_id = baseline(store, fake, snap)
    for w in range(5):
        wid = new_watch_id()
        store.conn.execute(
            "INSERT INTO watches VALUES (?, 'pr', ?, '2026-01-01T00:00:00.000000Z')", (wid, f"k{w}")
        )
        store.conn.execute("INSERT INTO owners VALUES (?, 'o')", (wid,))
        store.conn.execute("INSERT INTO sources (pr_id, watch_id) VALUES (?, ?)", (pr_id, wid))
        for i in range(64):
            store.conn.execute("INSERT INTO labels VALUES (?, 'o', ?)", (wid, f"team=l{w}-{i:03d}"))
    fake.update(snap["pr"], updated_at="2026-10-02T11:00:00Z")  # nothing an event is made of
    store.conn.execute("UPDATE prs SET listed_updated_at = '2026-10-02T11:00:00.000000Z'")
    store.conn.commit()
    log = Rec()
    out, _ = cycle(store, log=log)
    assert out.host(HOST).snapshots == 1 and out.host(HOST).events == 0
    assert log.of("labels_capped") == [] and log.of("commit_skipped") == []


def test_dropped_events_are_logged_as_events_dropped_not_commit_skipped(
    store: Store, fake: FakeForgectl, monkeypatch: pytest.MonkeyPatch
) -> None:
    snap = pr(1)
    fake.add(snap)
    pr_id = seed(store, snap)
    real = sn.plan_snapshot

    def with_drops(*args: Any, **kw: Any) -> Any:
        plan = real(*args, **kw)
        plan.events.dropped = (
            Drop("review", "invalid_review_id"),
            Drop("review", "invalid_review_id"),
            Drop("ci", "invalid_head_sha"),
        )
        return plan

    monkeypatch.setattr(sn, "plan_snapshot", with_drops)
    log = Rec()
    cycle(store, log=log)
    (dropped,) = log.of("events_dropped")
    assert dropped == {
        "pr": snap["pr"],
        "pr_id": pr_id,
        "count": 3,
        "reasons": ["invalid_head_sha", "invalid_review_id"],
    }
    assert [lvl for e, lvl, _ in log.records if e == "events_dropped"] == ["warn"]
    assert log.of("commit_skipped") == []


def test_a_snapshot_without_drops_writes_no_events_dropped(
    store: Store, fake: FakeForgectl
) -> None:
    snap = pr(1)
    fake.add(snap)
    seed(store, snap)
    log = Rec()
    cycle(store, log=log)
    assert log.of("events_dropped") == []


def test_a_stale_revision_is_a_conflict_and_the_other_commit_stays(
    store: Store, fake: FakeForgectl
) -> None:
    snap = pr(1)
    fake.add(snap)
    pr_id = seed(store, snap)

    class Racing(ForgectlClient):
        def snapshot(self, ref: PrRef) -> Outcome:
            out = super().snapshot(ref)
            other = sqlite3.connect(store.path)  # a watch add baseline won the race
            other.execute("UPDATE prs SET rev = rev + 1 WHERE pr_id = ?", (pr_id,))
            other.commit()
            other.close()
            return out

    base = make_client()
    racing = Racing(base.exe, timeout_ms=5000, max_output=4 * 1024 * 1024, env=dict(os.environ))
    out, _ = cycle(store, client=racing)
    assert out.host(HOST).skipped_conflict == 1 and out.host(HOST).snapshots == 0
    assert col(store, pr_id, "digest") is None


# --- unsettled and blind triggers ---


def test_ci_completion_that_updated_at_does_not_show_is_found_by_the_unsettled_rule(
    store: Store, fake: FakeForgectl
) -> None:
    snap = pr(1, ci={"state": "pending"})
    pr_id = baseline(store, fake, snap)
    assert col(store, pr_id, "unsettled_since") is not None
    first_start = col(store, pr_id, "unsettled_since")
    out, _ = cycle(store)  # still pending: chosen again, nothing new
    assert out.host(HOST).snapshots == 1 and col(store, pr_id, "unsettled_since") == first_start
    fake.update(snap["pr"], ci={"state": "passing"})  # same updated_at: the listing is blind
    out, _ = cycle(store)
    assert event_types(store) == ["ci"]
    assert col(store, pr_id, "unsettled_since") is None
    out, _ = cycle(store)
    assert out.needed == 0  # settled: no longer chosen


def test_a_push_resets_unsettled_since(store: Store, fake: FakeForgectl) -> None:
    snap = pr(1, ci={"state": "pending"})
    pr_id = baseline(store, fake, snap)
    hour_ago = format_ts(datetime.now(UTC) - timedelta(hours=1))
    store.conn.execute("UPDATE prs SET unsettled_since = ? WHERE pr_id = ?", (hour_ago, pr_id))
    store.conn.commit()
    fake.update(snap["pr"], head={"sha": sha(9)}, ci={"state": "pending"})
    cycle(store)
    assert col(store, pr_id, "unsettled_since") > hour_ago


def test_gitlab_style_blind_change_is_found_by_the_full_pass(
    store: Store, fake: FakeForgectl
) -> None:
    snap = pr(1)
    pr_id = baseline(store, fake, snap)
    fake.update(snap["pr"], approvals=3)  # no updated_at change
    store.conn.execute("UPDATE prs SET last_polled_at = '2026-01-01T00:00:00.000000Z'")
    store.conn.commit()
    out, _ = cycle(store)
    assert out.needed == 0
    started = datetime.now(UTC) - timedelta(minutes=1)
    out, _ = cycle(store, plan=sel.HostPlan(HOST, 4, pass_started_at=started))
    assert out.host(HOST).snapshots == 1 and col(store, pr_id, "rev") == 2


# --- failures ---


def test_counted_failures_make_a_pr_unreachable_once_and_never_an_event(
    store: Store, fake: FakeForgectl
) -> None:
    snap = pr(1)
    pr_id = baseline(store, fake, snap)
    digest = col(store, pr_id, "digest")
    fake.network_error("snapshot")
    cfg = Config.model_validate({"failures": {"unreachable_after": 2}})
    log = Rec()
    seen = []
    for _ in range(3):
        store.conn.execute(
            "UPDATE prs SET rebaseline = 0, listed_updated_at = ?", (NOW.isoformat(),)
        )
        store.conn.commit()
        out, res = cycle(store, cfg, log=log)
        seen.append(([u[2] for u in out.unreachable], col(store, pr_id, "status")))
        store.conn.execute("UPDATE prs SET status = status")  # keep the row chosen next time
        store.conn.execute("UPDATE prs SET status = 'active' WHERE status = 'unreachable'")
        store.conn.commit()
    assert [s[0] for s in seen] == [[], ["network_error"], []]  # reported once, at the crossing
    assert col(store, pr_id, "digest") == digest and event_types(store) == []
    assert col(store, pr_id, "last_failure_code") == "network_error"
    assert col(store, pr_id, "last_attempt_at") is not None
    assert [f["failures"] for f in log.of("snapshot_failed")] == [1, 2]  # first, then the multiple


def test_not_found_counts_and_never_closes_the_pr(store: Store, fake: FakeForgectl) -> None:
    snap = pr(1)
    pr_id = baseline(store, fake, snap)
    fake.remove(snap["pr"])
    store.conn.execute("UPDATE prs SET rebaseline = 1")
    store.conn.commit()
    out, res = cycle(store)
    assert col(store, pr_id, "failure_count") == 1 and event_types(store) == []
    assert col(store, pr_id, "status") == "active"
    assert [(f.kind, f.code) for f in res.failures] == [("pr", "not_found")]


def test_a_success_clears_the_counter_and_an_unreachable_row(
    store: Store, fake: FakeForgectl
) -> None:
    snap = pr(1)
    fake.add(snap)
    pr_id = seed(store, snap, status="unreachable", failure_count=5, last_failure_code="not_found")
    log = Rec()
    out, _ = cycle(
        store, plan=sel.HostPlan(HOST, 4, pass_started_at=NOW - timedelta(minutes=1)), log=log
    )
    assert col(store, pr_id, "status") == "active" and col(store, pr_id, "failure_count") == 0
    assert log.of("pr_status")[0] == {
        "pr": snap["pr"],
        "pr_id": pr_id,
        "from": "unreachable",
        "to": "active",
    }


def test_rate_limited_pauses_the_host_without_counting_and_cancels_the_queue(
    store: Store, fake: FakeForgectl
) -> None:
    ids = []
    for n in range(1, 7):
        s = pr(n)
        fake.add(s)
        ids.append(seed(store, s))
    fake.rate_limited("snapshot", retry_after=30)
    out, res = cycle(store, plan=sel.HostPlan(HOST, 1))
    facts = res.facts(HOST)
    assert facts.rate_limited and facts.retry_after == 30 and facts.cancelled
    assert all(col(store, i, "failure_count") == 0 for i in ids)
    assert out.host(HOST).calls <= 2  # the queue was cancelled after the first answer
    assert event_types(store) == [] and lease_count(store) == 0


def test_a_host_wide_failure_stops_new_calls_without_waiting_for_the_oldest_result(
    store: Store, fake: FakeForgectl
) -> None:
    for n in range(1, 31):
        s = pr(n)
        fake.add(s)
        seed(store, s, last_polled_at=f"2026-09-{n:02d}T00:00:00.000000Z")
    fake.slow("snapshot", 1.5, pr=pr(1)["pr"])  # the head of the queue answers late
    fake.rate_limited("snapshot", retry_after=30)
    out, res = cycle(store, plan=sel.HostPlan(HOST, 4))
    assert res.facts(HOST).rate_limited and res.facts(HOST).cancelled
    assert len(fake.calls("snapshot")) <= 4  # no refill after the first limited answer
    assert lease_count(store) == 0 and event_types(store) == []


def test_auth_required_fails_the_host_once_for_all_its_queued_prs(
    store: Store, fake: FakeForgectl
) -> None:
    for n in range(1, 7):
        s = pr(n)
        fake.add(s)
        seed(store, s)
    fake.auth_required("snapshot")
    out, res = cycle(store, plan=sel.HostPlan(HOST, 2))
    assert [(f.kind, f.ref, f.poll_exit) for f in res.failures] == [("host", HOST, 12)]
    assert store.conn.execute("SELECT sum(failure_count) FROM prs").fetchone()[0] == 0


def test_a_worker_exception_loses_only_that_pr(store: Store, fake: FakeForgectl) -> None:
    ids = []
    for n in range(1, 5):
        s = pr(n)
        fake.add(s)
        ids.append(seed(store, s))

    class Boom(ForgectlClient):
        def snapshot(self, ref: PrRef) -> Outcome:
            if ref.number == 2:
                raise RuntimeError("bug")
            return super().snapshot(ref)

    base = make_client()
    client = Boom(base.exe, timeout_ms=5000, max_output=4 * 1024 * 1024, env=dict(os.environ))
    out, res = cycle(store, client=client)
    assert out.host(HOST).snapshots == 3 and lease_count(store) == 0
    bad = next(i for i in ids if "#2" in col(store, i, "pr"))
    assert (
        col(store, bad, "failure_count") == 1 and col(store, bad, "last_failure_code") == "internal"
    )


def test_a_snapshot_with_an_unreadable_updated_at_is_a_counted_contract_failure(
    store: Store, fake: FakeForgectl
) -> None:
    s = pr(1)
    fake.add(s)
    pr_id = seed(store, s)

    class Odd(ForgectlClient):
        def snapshot(self, ref: PrRef) -> Outcome:
            out = super().snapshot(ref)
            assert out.result is not None
            return dataclasses.replace(out, result={**out.result, "updated_at": "yesterday"})

    base = make_client()
    odd = Odd(base.exe, timeout_ms=5000, max_output=4 * 1024 * 1024, env=dict(os.environ))
    cycle(store, client=odd)
    assert (
        col(store, pr_id, "last_failure_code") == "contract" and col(store, pr_id, "digest") is None
    )


def _due_pr(store: Store, fake: FakeForgectl) -> tuple[dict[str, Any], str]:
    snap = pr(1)
    pr_id = baseline(store, fake, snap)
    store.conn.execute("UPDATE prs SET listed_updated_at = '2026-10-02T11:00:00.000000Z'")
    store.conn.commit()
    return snap, pr_id


class _Racy(Leases):
    """Another poll commits this PR between the choice and the lease."""

    def __init__(self, store: Store) -> None:
        super().__init__(store, TTL)
        self._store = store

    def acquire(self, pr_id: str) -> Any:
        self._store.conn.execute("UPDATE prs SET snap_updated_at = listed_updated_at")
        self._store.conn.commit()
        return super().acquire(pr_id)


def _choose(store: Store) -> sel.Selection:
    return sel.choose_snapshots(
        store.conn, {HOST: sel.HostPlan(HOST, 4)}, now=datetime.now(UTC), pending_max_age_ms=10**9
    )


def test_a_task_whose_trigger_a_concurrent_poll_cleared_is_skipped(
    store: Store, fake: FakeForgectl
) -> None:
    _, pr_id = _due_pr(store, fake)
    before = len(fake.calls("snapshot"))
    chosen = _choose(store)
    assert chosen.total == 1
    res = en.EnumerationResult(en.HostList((), frozenset(), True))
    out = sn.run_snapshots(
        store, make_client(), Config(), chosen, res, concurrency={HOST: 4}, leases=_Racy(store)
    )
    assert out.host(HOST).snapshots == 0 and out.host(HOST).calls == 0
    assert len(fake.calls("snapshot")) == before and lease_count(store) == 0
    assert col(store, pr_id, "rev") == 1


def test_a_forced_task_is_never_skipped_by_the_recheck(store: Store, fake: FakeForgectl) -> None:
    _, pr_id = _due_pr(store, fake)
    chosen = _choose(store)
    chosen.tasks[HOST] = [dataclasses.replace(t, forced=True) for t in chosen.tasks[HOST]]
    res = en.EnumerationResult(en.HostList((), frozenset(), True))
    out = sn.run_snapshots(
        store, make_client(), Config(), chosen, res, concurrency={HOST: 4}, leases=_Racy(store)
    )
    assert out.host(HOST).snapshots == 1 and col(store, pr_id, "rev") == 2


@pytest.mark.parametrize("boom", ["load", "submit"])
def test_a_lease_is_released_when_starting_the_snapshot_raises(
    store: Store, fake: FakeForgectl, monkeypatch: pytest.MonkeyPatch, boom: str
) -> None:
    s = pr(1)
    fake.add(s)
    seed(store, s)

    def raising(*args: Any, **kw: Any) -> Any:
        raise RuntimeError("boom")

    if boom == "load":
        monkeypatch.setattr(sn._Pool, "_load", raising)
    else:
        monkeypatch.setattr(sn.ThreadPoolExecutor, "submit", raising)
    with pytest.raises(RuntimeError, match="boom"):
        cycle(store)
    assert lease_count(store) == 0


# --- leases ---


def hold(store: Store, pr_id: str) -> None:
    other = ProcFs().identity(1)
    store.conn.execute(
        "INSERT INTO leases VALUES (?, ?, ?, ?, ?)",
        (pr_id, other.pid, other.start, other.boot_id, format_ts(datetime.now(UTC) + TTL)),
    )
    store.conn.commit()


def test_a_pr_leased_by_a_live_other_poll_is_skipped_locked(
    store: Store, fake: FakeForgectl
) -> None:
    a, b = pr(1), pr(2)
    for s in (a, b):
        fake.add(s)
    ia = seed(store, a)
    seed(store, b)
    hold(store, ia)
    out, _ = cycle(store)
    assert out.host(HOST).skipped_locked == 1 and out.host(HOST).snapshots == 1
    assert col(store, ia, "digest") is None and not out.already_running
    assert lease_count(store) == 1  # the other poll's lease is untouched


def test_already_running_when_every_needed_pr_is_leased(store: Store, fake: FakeForgectl) -> None:
    ids = []
    for n in (1, 2):
        s = pr(n)
        fake.add(s)
        ids.append(seed(store, s))
    for i in ids:
        hold(store, i)
    out, _ = cycle(store)
    assert out.already_running and out.skipped_locked == 2 and len(fake.calls("snapshot")) == 0


# --- order, budget, signals ---


def test_commits_follow_the_start_order_even_when_the_first_is_the_slowest(
    store: Store, fake: FakeForgectl
) -> None:
    for n in range(1, 6):
        s = pr(n, state="closed", closed_at="2026-10-02T10:00:00Z")
        fake.add(s)
        seed(store, s, last_polled_at=f"2026-10-0{n}T00:00:00.000000Z")
    fake.slow("snapshot", 0.6, pr=pr(1)["pr"])
    out, _ = cycle(store, plan=sel.HostPlan(HOST, 4))
    seqs = [r[0] for r in store.conn.execute("SELECT pr FROM events ORDER BY seq")]
    assert seqs == [pr(n)["pr"] for n in range(1, 6)]
    assert out.host(HOST).events == 5


def test_out_of_time_stops_starting_and_defers_the_rest(store: Store, fake: FakeForgectl) -> None:
    for n in range(1, 8):
        s = pr(n)
        fake.add(s)
        seed(store, s)
    calls = {"n": 0}

    def budget() -> bool:
        calls["n"] += 1
        return calls["n"] > 3

    out, _ = cycle(store, plan=sel.HostPlan(HOST, 1), out_of_time=budget)
    assert out.truncated and out.host(HOST).snapshots == 3 and out.deferred == 4
    assert not out.interrupted and lease_count(store) == 0


def test_a_stop_signal_commits_nothing_more_and_releases_the_leases(
    store: Store, fake: FakeForgectl
) -> None:
    ids = []
    for n in range(1, 6):
        s = pr(n)
        fake.add(s)
        ids.append(seed(store, s))
    stop = threading.Event()

    class Stopper(ForgectlClient):
        def snapshot(self, ref: PrRef) -> Outcome:
            out = super().snapshot(ref)
            stop.set()
            time.sleep(0.05)
            return out

    base = make_client()
    client = Stopper(base.exe, timeout_ms=5000, max_output=4 * 1024 * 1024, env=dict(os.environ))
    out, _ = cycle(store, client=client, plan=sel.HostPlan(HOST, 2), should_stop=stop.is_set)
    assert out.interrupted and lease_count(store) == 0
    assert out.host(HOST).snapshots == 0
    assert all(col(store, i, "digest") is None and col(store, i, "rev") == 0 for i in ids)


def test_many_prs_run_with_bounded_workers(store: Store, fake: FakeForgectl) -> None:
    for n in range(1, 31):
        s = pr(n)
        fake.add(s)
        seed(store, s)
    out, _ = cycle(store, plan=sel.HostPlan(HOST, 3))
    assert out.host(HOST).snapshots == 30 and lease_count(store) == 0
    assert len(fake.calls("snapshot")) == 30


def test_a_tool_failure_stops_the_cycle(store: Store, fake: FakeForgectl) -> None:
    s = pr(1)
    fake.add(s)
    pr_id = seed(store, s)
    fake.version("forgectl 0.1.0")
    out, _ = cycle(store, client=make_client())
    assert out.tool_error is not None and col(store, pr_id, "failure_count") == 0


# --- two concurrent polls (processes) ---


def test_two_concurrent_pools_store_each_event_once(
    tmp_path: Path, fake: FakeForgectl, store: Store
) -> None:
    import subprocess
    import sys

    ids = []
    for n in range(1, 9):
        s = pr(n, state="closed", closed_at="2026-10-02T10:00:00Z")
        fake.add(s)
        ids.append(seed(store, s))
    store.conn.commit()
    go = tmp_path / "go"
    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": f"{root / 'src'}:{root}"}
    cmd = [sys.executable, str(root / "tests" / "pool_proc.py"), str(store.path.parent), str(go)]
    procs = [
        subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, text=True, cwd=root)
        for _ in range(2)
    ]
    time.sleep(1.0)  # both have chosen their snapshots and wait at the gate
    go.write_text("go")
    outs = []
    for p in procs:
        out, _ = p.communicate(timeout=120)
        assert p.returncode == 0
        outs.append(json.loads(out.strip().splitlines()[-1]))
    assert len({o["pid"] for o in outs}) == 2 and all(o["pid"] != os.getpid() for o in outs)
    assert all(o["failures"] == [] for o in outs)
    per_pr = [r[0] for r in store.conn.execute("SELECT pr_id FROM events WHERE type = 'closed'")]
    assert sorted(per_pr) == sorted(ids)  # every PR's `closed` event exactly once
    assert all(col(store, i, "status") == "terminal" for i in ids)
    assert lease_count(store) == 0
    assert sum(o["snapshots"] for o in outs) >= 8
    assert sum(o["snapshots"] + o["locked"] + o["conflict"] for o in outs) >= 8


def test_lease_ttl_is_three_times_the_forgectl_timeout(store: Store) -> None:
    assert sn.make_leases(store, Config()).ttl == timedelta(minutes=6)
