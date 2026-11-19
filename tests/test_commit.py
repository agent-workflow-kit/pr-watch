"""The compare-and-set commit: a digest, its events and its fields land together or not at all."""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from pr_watch.commit import CommitResult, DiscardReason, commit_snapshot
from pr_watch.state_dir import StateDir, open_state_dir
from pr_watch.store import EventError, NewEvent, Store, open_store

PR = "github.com/o/r#1"
PR_ID = "PR_abc123"
WATCH = "w-aaaaaaaaaa"
SHA = "a" * 40
COMMON: dict[str, Any] = {
    "repo": "github.com/o/r",
    "occurred_at": None,
    "head_sha": SHA,
    "watches": [],
}


@pytest.fixture
def state(tmp_path: Path) -> Iterator[StateDir]:
    with open_state_dir(tmp_path / "pr-watch") as s:
        yield s


@pytest.fixture
def store(state: StateDir) -> Iterator[Store]:
    with open_store(state) as s:
        yield s


def seed(store: Store, pr_id: str = PR_ID, listed: str | None = None) -> None:
    c = store.conn
    c.execute(
        "INSERT OR IGNORE INTO watches VALUES (?, 'pr', ?, '2026-01-01T00:00:00.000000Z')",
        (WATCH, PR),
    )
    c.execute(
        "INSERT INTO prs (pr_id, pr, repo, host, listed_updated_at) VALUES (?, ?, 'r', 'h', ?)",
        (pr_id, PR, listed),
    )
    c.execute("INSERT INTO sources (pr_id, watch_id) VALUES (?, ?)", (pr_id, WATCH))


def event(kind: str = "closed") -> NewEvent:
    return NewEvent(type=kind, pr_id=PR_ID, pr=PR, payload=dict(COMMON))


def prs_row(store: Store) -> sqlite3.Row | tuple[Any, ...] | None:
    return store.conn.execute(
        "SELECT rev, digest, snap_updated_at, status FROM prs WHERE pr_id = ?", (PR_ID,)
    ).fetchone()


def event_count(store: Store) -> int:
    return int(store.conn.execute("SELECT count(*) FROM events").fetchone()[0])


def commit(store: Store, rev: int = 0, **kw: Any) -> CommitResult:
    kw.setdefault("fields", {"digest": '{"v":1}'})
    kw.setdefault("snapshot_pr_id", PR_ID)
    return commit_snapshot(store, pr_id=PR_ID, expected_rev=rev, **kw)


def test_a_commit_writes_fields_events_and_bumps_the_revision(store: Store) -> None:
    seed(store)
    result = commit(
        store,
        fields={"digest": '{"v":1}', "snap_updated_at": "2026-01-02T00:00:00Z"},
        prepare=lambda conn: [event("closed"), event("merged")],
    )
    assert result.committed and result.reason is None and result.new_rev == 1
    assert [e.seq for e in result.events] == [1, 2]
    assert tuple(prs_row(store) or ()) == (1, '{"v":1}', "2026-01-02T00:00:00Z", "active")
    assert event_count(store) == 2


def test_a_commit_without_events_still_commits(store: Store) -> None:
    seed(store)
    assert commit(store).committed
    assert event_count(store) == 0


def test_a_changed_revision_discards_everything(store: Store) -> None:
    seed(store)
    assert commit(store, rev=0).committed
    called: list[int] = []
    result = commit(store, rev=0, prepare=lambda conn: called.append(1) or [event()])
    assert not result.committed and result.reason is DiscardReason.REVISION
    assert result.counts_as_conflict and result.events == () and result.new_rev is None
    assert called == []  # a discarded snapshot never even builds its events
    assert tuple(prs_row(store) or ())[:2] == (1, '{"v":1}')
    assert event_count(store) == 0


def test_a_removed_last_source_discards_the_commit(store: Store) -> None:
    seed(store)
    with store.transaction() as conn:
        conn.execute("DELETE FROM sources WHERE pr_id = ?", (PR_ID,))  # drops the prs row too
    result = commit(store, prepare=lambda conn: [event()])
    assert result.reason is DiscardReason.SOURCE_REMOVED and result.counts_as_conflict
    assert event_count(store) == 0
    assert prs_row(store) is None  # no resurrection


def test_a_prs_row_without_sources_is_discarded(store: Store) -> None:
    seed(store)
    store.conn.execute("DROP TRIGGER sources_drop_orphan_pr")
    store.conn.execute("DELETE FROM sources")
    result = commit(store)
    assert result.reason is DiscardReason.SOURCE_REMOVED
    assert tuple(prs_row(store) or ())[0] == 0


def test_a_snapshot_of_another_pr_id_is_discarded(store: Store) -> None:
    seed(store)
    result = commit(store, snapshot_pr_id="PR_other", prepare=lambda c: [event()])
    assert result.reason is DiscardReason.PR_ID_MISMATCH
    assert tuple(prs_row(store) or ())[0] == 0 and event_count(store) == 0


def test_a_closed_snapshot_older_than_a_listing_is_discarded_without_a_conflict(
    store: Store,
) -> None:
    seed(store, listed="2026-01-03T00:00:00Z")
    result = commit(
        store,
        fields={
            "terminal_kind": "closed",
            "status": "terminal",
            "snap_updated_at": "2026-01-02T00:00:00Z",
        },
        snapshot_updated_at="2026-01-02T00:00:00Z",
        prepare=lambda c: [event()],
    )
    assert result.reason is DiscardReason.NEWER_LISTING and not result.counts_as_conflict
    assert tuple(prs_row(store) or ())[3] == "active" and event_count(store) == 0


def test_a_closed_snapshot_at_or_after_the_listing_commits(store: Store) -> None:
    seed(store, listed="2026-01-02T00:00:00Z")
    result = commit(
        store,
        fields={
            "terminal_kind": "closed",
            "status": "terminal",
            "snap_updated_at": "2026-01-02T00:00:00Z",
        },
        snapshot_updated_at="2026-01-02T00:00:00Z",
    )
    assert result.committed
    assert tuple(prs_row(store) or ())[3] == "terminal"


def test_a_closed_snapshot_with_unparseable_times_is_discarded(store: Store) -> None:
    seed(store, listed="not a time")
    result = commit(
        store,
        fields={"terminal_kind": "closed", "snap_updated_at": "2026-01-02T00:00:00Z"},
        snapshot_updated_at="2026-01-02T00:00:00Z",
    )
    assert result.reason is DiscardReason.NEWER_LISTING


def test_a_lagging_closed_snapshot_is_discarded_even_when_snap_updated_at_is_larger(
    store: Store,
) -> None:
    seed(store, listed="2026-01-03T00:00:00Z")
    result = commit(
        store,
        fields={"terminal_kind": "closed", "snap_updated_at": "2026-01-04T00:00:00Z"},
        snapshot_updated_at="2026-01-02T00:00:00Z",
    )
    assert result.reason is DiscardReason.NEWER_LISTING


def test_a_closed_snapshot_without_its_own_updated_at_is_a_caller_bug(store: Store) -> None:
    seed(store)
    with pytest.raises(ValueError, match="snapshot_updated_at"):
        commit(store, fields={"terminal_kind": "closed"})
    assert tuple(prs_row(store) or ())[0] == 0


def test_a_naive_snapshot_time_is_discarded_as_newer_listing(store: Store) -> None:
    seed(store, listed="2026-01-02T00:00:00Z")
    result = commit(
        store,
        fields={"terminal_kind": "closed"},
        snapshot_updated_at="2026-01-02T00:00:00",
    )
    assert result.reason is DiscardReason.NEWER_LISTING


def test_a_pr_id_mismatch_is_not_a_conflict_count(store: Store) -> None:
    seed(store)
    assert not commit(store, snapshot_pr_id="PR_other").counts_as_conflict


def test_a_non_scalar_field_value_is_a_type_error_naming_the_field(store: Store) -> None:
    seed(store)
    with pytest.raises(TypeError, match="digest"):
        commit(store, fields={"digest": {"a": 1}})
    assert tuple(prs_row(store) or ())[0] == 0


def test_a_prepare_that_removes_the_last_source_commits_events_and_drops_the_row(
    store: Store,
) -> None:
    seed(store)
    store.conn.execute("UPDATE sources SET left_count = 2")

    def drop(conn: sqlite3.Connection) -> Sequence[NewEvent]:
        conn.execute("DELETE FROM sources WHERE pr_id = ?", (PR_ID,))
        return [event()]

    result = commit(store, prepare=drop)
    assert result.committed and result.new_rev is None and len(result.events) == 1
    assert event_count(store) == 1
    assert prs_row(store) is None


def test_a_non_closed_snapshot_ignores_the_listing_time(store: Store) -> None:
    seed(store, listed="2026-01-03T00:00:00Z")
    assert commit(store, fields={"snap_updated_at": "2026-01-02T00:00:00Z"}).committed


def test_a_failing_prepare_rolls_back_the_whole_commit(store: Store) -> None:
    seed(store)

    def boom(conn: sqlite3.Connection) -> Sequence[NewEvent]:
        conn.execute("UPDATE prs SET failure_count = 9")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        commit(store, prepare=boom)
    row = store.conn.execute("SELECT rev, failure_count FROM prs").fetchone()
    assert tuple(row) == (0, 0)


def test_an_invalid_event_rolls_back_the_fields_too(store: Store) -> None:
    seed(store)
    bad = NewEvent(type="nope", pr_id=PR_ID, pr=PR, payload=dict(COMMON))
    with pytest.raises(EventError):
        commit(store, prepare=lambda c: [event(), bad])
    assert tuple(prs_row(store) or ())[0] == 0 and event_count(store) == 0


def test_prepare_may_write_in_the_same_transaction(store: Store) -> None:
    seed(store)
    commit(store, prepare=lambda c: c.execute("UPDATE sources SET left_count = 1") and [])
    assert store.conn.execute("SELECT left_count FROM sources").fetchone()[0] == 1


@pytest.mark.parametrize(
    "key",
    [
        "rev",
        "pr_id",
        "pr",
        "repo",
        "host",
        "listed_updated_at",
        "last_attempt_at",
        "last_failure_code",
        "pr; DROP TABLE prs",
        "nope",
        "",
        "digest = 1 --",
    ],
)
def test_only_known_digest_columns_may_be_written(store: Store, key: str) -> None:
    seed(store)
    with pytest.raises(ValueError):
        commit(store, fields={key: "x"})
    assert tuple(prs_row(store) or ())[0] == 0


def test_a_commit_inside_an_open_transaction_is_refused(store: Store) -> None:
    seed(store)
    with store.transaction(), pytest.raises(Exception, match="already open"):
        commit(store)


# --- processes -------------------------------------------------------------------------------

WRITER = """
    import os, sys, time
    from pathlib import Path
    from pr_watch.commit import commit_snapshot
    from pr_watch.state_dir import open_state_dir
    from pr_watch.store import NewEvent, open_store
    st = open_state_dir(Path(sys.argv[1]))
    s = open_store(st)
    def prepare(conn):
        evs = [NewEvent("closed", "PR_abc123", "github.com/o/r#1",
               {"repo": "github.com/o/r", "occurred_at": None, "head_sha": "a" * 40, "watches": []})
               for _ in range(3)]
        return evs
    def stall(statement):
        # fires before the statement runs: everything is written but not yet committed
        if statement == "COMMIT":
            print("in-transaction", flush=True)
            time.sleep(120)
    s.conn.set_trace_callback(stall)
    commit_snapshot(s, pr_id="PR_abc123", expected_rev=0, snapshot_pr_id="PR_abc123",
                    fields={"digest": "new"}, prepare=prepare)
"""


def test_killing_the_process_mid_commit_leaves_no_partial_state(state: StateDir) -> None:
    with open_store(state) as s:
        seed(s)
    child = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(WRITER), str(state.path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pid = child.pid
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "in-transaction"
        os.kill(pid, signal.SIGKILL)  # the exact child we started
        child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)
    with open_store(state, busy_timeout_ms=15000) as s:
        row = s.conn.execute("SELECT rev, digest FROM prs WHERE pr_id = ?", (PR_ID,)).fetchone()
        assert tuple(row) == (0, None)
        assert event_count(s) == 0
        assert commit(s).committed  # the lock died with the process; the next commit works


def test_a_second_connection_with_a_stale_revision_is_discarded(state: StateDir) -> None:
    with open_store(state) as a, open_store(state) as b:
        seed(a)
        first = commit(a, fields={"digest": "A"}, prepare=lambda c: [event()])
        second = commit(b, fields={"digest": "B"}, prepare=lambda c: [event()])
        assert first.committed and not second.committed
        assert second.reason is DiscardReason.REVISION
        assert event_count(a) == 1
        assert a.conn.execute("SELECT digest FROM prs").fetchone()[0] == "A"


BARRIER_WRITER = """
    import os, sys, time
    from pathlib import Path
    from pr_watch.commit import commit_snapshot
    from pr_watch.state_dir import open_state_dir
    from pr_watch.store import NewEvent, open_store
    st = open_state_dir(Path(sys.argv[1]))
    s = open_store(st, busy_timeout_ms=20000)
    print("ready", flush=True)
    deadline = time.monotonic() + 60
    while not os.path.exists(sys.argv[2]):  # the barrier file: released together
        assert time.monotonic() < deadline
        time.sleep(0.001)
    r = commit_snapshot(
        s, pr_id="PR_abc123", expected_rev=0, snapshot_pr_id="PR_abc123",
        fields={"digest": sys.argv[3]},
        prepare=lambda conn: [NewEvent("closed", "PR_abc123", "github.com/o/r#1",
            {"repo": "github.com/o/r", "occurred_at": None, "head_sha": "a" * 40, "watches": []})],
    )
    print("committed" if r.committed else r.reason.value, flush=True)
"""


def test_two_processes_released_together_commit_the_same_revision_once(
    state: StateDir, tmp_path: Path
) -> None:
    with open_store(state) as s:
        seed(s)
    barrier = tmp_path / "go"
    env = {**os.environ, "HOME": str(tmp_path / "h"), "XDG_STATE_HOME": str(tmp_path / "x")}
    env["XDG_CONFIG_HOME"] = str(tmp_path / "c")
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                textwrap.dedent(BARRIER_WRITER),
                str(state.path),
                str(barrier),
                d,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for d in ("A", "B")
    ]
    try:
        for p in procs:
            assert p.stdout is not None and p.stdout.readline().strip() == "ready"
        barrier.write_text("go")
        results = []
        for p in procs:
            assert p.stdout is not None
            results.append(p.stdout.readline().strip())
            p.wait(timeout=60)
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=30)
    assert sorted(results) == ["committed", "revision"]
    with open_store(state) as s:
        assert event_count(s) == 1
        assert tuple(prs_row(s) or ())[0] == 1
