"""Watches, owners, labels, members and expiry on the store tables."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pr_watch import watches as w
from pr_watch.commit import DiscardReason, commit_snapshot
from pr_watch.refs import Label, PrId, RefError, parse_pr
from pr_watch.state_dir import open_state_dir
from pr_watch.store import Store, format_ts, open_store
from tests.harness import assert_valid

NOW = datetime(2026, 6, 1, tzinfo=UTC)
A, B = "human:a", "agent:b"
L1, L2 = Label("k", "v"), Label("x", "y")


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with open_state_dir(tmp_path / "pr-watch") as state, open_store(state) as s:
        yield s


def add_pr(conn: sqlite3.Connection, pr_id: str, pr: str, digest: str | None = "{}") -> None:
    conn.execute(
        "INSERT INTO prs (pr_id, pr, repo, host, digest) VALUES (?, ?, ?, ?, ?)",
        (pr_id, pr, pr.split("#")[0], pr.split("/")[0], digest),
    )


def pr_watch(conn: sqlite3.Connection, pr_id: str, pr: str, *owners: str) -> str:
    add_pr(conn, pr_id, pr)
    wid, _ = w.ensure_watch(conn, "pr", pr_id, NOW)
    w.add_source(conn, pr_id, wid)
    for o in owners:
        w.add_owner(conn, wid, o)
    return wid


def count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # noqa: S608


def test_watch_ids_match_the_schema() -> None:
    for _ in range(20):
        assert_valid("defs", w.new_watch_id(), "watch_id")


def test_ensure_watch_is_idempotent_per_key(store: Store) -> None:
    with store.transaction() as c:
        first = w.ensure_watch(c, "query", "mine+open", NOW)
        again = w.ensure_watch(c, "query", "mine+open", NOW)
    assert first[1] is True and again == (first[0], False)


def test_owners_and_labels_add_idempotently(store: Store) -> None:
    with store.transaction() as c:
        wid = pr_watch(c, "P1", "h/o/r#1", A)
        assert w.add_owner(c, wid, A) is False
        assert w.add_labels(c, wid, A, [L1, L2]) is True
        assert w.add_labels(c, wid, A, [L1]) is False
        assert w.labels_of(c, wid) == ["k=v", "x=y"]


def test_labels_need_a_held_reference(store: Store) -> None:
    with store.transaction() as c:
        wid = pr_watch(c, "P1", "h/o/r#1", A)
        with pytest.raises(sqlite3.IntegrityError):
            w.add_labels(c, wid, B, [L1])


def test_labels_are_a_union_but_removal_is_per_owner(store: Store) -> None:
    with store.transaction() as c:
        wid = pr_watch(c, "P1", "h/o/r#1", A, B)
        w.add_labels(c, wid, A, [L1])
        w.add_labels(c, wid, B, [L1, L2])
        assert w.labels_of(c, wid) == ["k=v", "x=y"]
        assert w.remove_labels(c, wid, A, [L1]) is True
        assert w.remove_labels(c, wid, A, [L1]) is False
        assert w.labels_of(c, wid) == ["k=v", "x=y"]  # B still holds k=v
        assert w.labels_of(c, wid, A) == []


def test_removing_an_owner_keeps_the_others_and_drops_its_labels(store: Store) -> None:
    with store.transaction() as c:
        wid = pr_watch(c, "P1", "h/o/r#1", A, B)
        w.add_labels(c, wid, A, [L1])
        w.add_labels(c, wid, B, [L2])
        out = w.remove_owner(c, wid, A)
        assert (out.held, out.watch_ended, out.prs_dropped) == (True, False, 0)
        assert w.labels_of(c, wid) == ["x=y"]
        assert count(c, "prs") == 1


def test_removing_an_absent_owner_changes_nothing(store: Store) -> None:
    with store.transaction() as c:
        wid = pr_watch(c, "P1", "h/o/r#1", A)
        out = w.remove_owner(c, wid, B)
        assert (out.held, out.watch_ended) == (False, False)
        assert w.holds(c, wid, A)


def test_the_last_owner_ends_the_watch_and_drops_orphan_rows(store: Store) -> None:
    with store.transaction() as c:
        wid = pr_watch(c, "P1", "h/o/r#1", A)
        w.add_labels(c, wid, A, [L1])
        out = w.remove_owner(c, wid, A)
        assert (out.held, out.watch_ended, out.prs_dropped) == (True, True, 1)
        for table in ("watches", "owners", "labels", "sources", "prs"):
            assert count(c, table) == 0


def test_a_pr_shared_with_another_watch_keeps_its_row_and_digest(store: Store) -> None:
    with store.transaction() as c:
        wid = pr_watch(c, "P1", "h/o/r#1", A)
        q, _ = w.ensure_watch(c, "query", "mine+open", NOW)
        w.add_owner(c, q, B)
        w.add_source(c, "P1", q)
        out = w.remove_owner(c, wid, A)
        assert (out.watch_ended, out.prs_dropped) == (True, 0)
        assert c.execute("SELECT digest FROM prs").fetchone()[0] == "{}"


def test_a_query_watch_drops_only_prs_no_other_source_holds(store: Store) -> None:
    with store.transaction() as c:
        per_pr = pr_watch(c, "P1", "h/o/r#1", A)
        q, _ = w.ensure_watch(c, "query", "mine+open", NOW)
        w.add_owner(c, q, A)
        add_pr(c, "P2", "h/o/r#2")
        for pid in ("P1", "P2"):
            w.add_source(c, pid, q)
        out = w.remove_owner(c, q, A)
        assert out.prs_dropped == 1
        assert w.pr_watch_id(c, "P1") == per_pr


def test_resolve_matches_case_insensitively_and_by_pr_id(store: Store) -> None:
    with store.transaction() as c:
        add_pr(c, "P1", "h/o/r#1")
        assert w.resolve_stored_pr(c, parse_pr("H/O/R#1")) == ("P1", "h/o/r#1")
        assert w.resolve_stored_pr(c, PrId("P1")) == ("P1", "h/o/r#1")
        assert w.resolve_stored_pr(c, PrId("P2")) is None
        assert w.resolve_stored_pr(c, parse_pr("h/o/r#2")) is None


def test_resolve_refuses_a_name_shared_by_two_rows(store: Store) -> None:
    with store.transaction() as c:
        add_pr(c, "P1", "h/o/r#1")
        add_pr(c, "P2", "h/o/r#1")
        with pytest.raises(RefError) as exc:
            w.resolve_stored_pr(c, parse_pr("h/o/r#1"))
        assert exc.value.kind == "ambiguous_reference"
        assert w.resolve_stored_pr(c, PrId("P2")) == ("P2", "h/o/r#1")


def test_watch_view_validates_against_the_schema(store: Store) -> None:
    with store.transaction() as c:
        wid = pr_watch(c, "P1", "h/o/r#1", B, A)
        w.add_labels(c, wid, A, [L1])
        view = w.watch_view(c, wid)
        q, _ = w.ensure_watch(c, "query", "mine+open;host=h", NOW)
        w.add_owner(c, q, A)
        add_pr(c, "P2", "h/o/r#2", digest=None)
        w.add_source(c, "P2", q)
        qview = w.watch_view(c, q)
    assert view is not None and qview is not None
    assert_valid("defs", view, "watch")
    assert_valid("defs", qview, "watch")
    assert view["owners"] == [B, A] or view["owners"] == sorted([A, B])
    assert view["pr"] == "h/o/r#1" and view["members"] is None
    assert (qview["members"], qview["pending_baseline"], qview["enumerated_at"]) == (1, 1, None)


def test_enumerated_at_needs_every_listed_host_enumerated_after_creation(store: Store) -> None:
    later = format_ts(NOW + timedelta(hours=1))
    with store.transaction() as c:
        q, _ = w.ensure_watch(c, "query", "mine+open;host=a.com,b.com", NOW)
        w.add_owner(c, q, A)
        c.execute("INSERT INTO hosts (host, last_enumerated_at) VALUES ('a.com', ?)", (later,))
        assert (w.watch_view(c, q) or {})["enumerated_at"] is None  # b.com unknown
        c.execute(
            "INSERT INTO hosts (host, last_enumerated_at) VALUES ('b.com', ?)",
            (format_ts(NOW - timedelta(hours=1)),),
        )
        assert (w.watch_view(c, q) or {})["enumerated_at"] is None  # older than the watch
        c.execute("UPDATE hosts SET last_enumerated_at = ? WHERE host = 'b.com'", (later,))
        assert (w.watch_view(c, q) or {})["enumerated_at"] == later


def test_watch_view_of_a_missing_watch_is_none(store: Store) -> None:
    with store.transaction() as c:
        assert w.watch_view(c, "w-aaaaaaaaaa") is None


def test_expiry_predicate_and_cutoff(store: Store) -> None:
    cutoff = w.expiry_cutoff(NOW, "7d")
    assert cutoff == format_ts(NOW - timedelta(days=7))
    old, new = format_ts(NOW - timedelta(days=8)), format_ts(NOW - timedelta(days=6))
    assert w.is_expired(old, cutoff) and not w.is_expired(new, cutoff)
    assert not w.is_expired(None, cutoff) and not w.is_expired(cutoff, cutoff)
    with store.transaction() as c:
        for pid, status, at in (
            ("T1", "terminal", old),
            ("T2", "terminal", new),
            ("U1", "unreachable", old),
            ("A1", "active", None),
        ):
            c.execute(
                "INSERT INTO prs (pr_id, pr, repo, host, status, terminal_at) "
                "VALUES (?, 'h/o/r#1', 'h/o/r', 'h', ?, ?)",
                (pid, status, at),
            )
        rows = c.execute(f"SELECT pr_id FROM prs WHERE {w.EXPIRED_PREDICATE}", (cutoff,)).fetchall()
    assert [r[0] for r in rows] == ["T1"]


def test_a_commit_in_flight_is_discarded_after_the_last_owner_left(store: Store) -> None:
    with store.transaction() as c:
        wid = pr_watch(c, "P1", "h/o/r#1", A)
        w.remove_owner(c, wid, A)
    result = commit_snapshot(
        store, pr_id="P1", expected_rev=0, snapshot_pr_id="P1", fields={"digest": '{"v":1}'}
    )
    assert not result.committed and result.reason is DiscardReason.SOURCE_REMOVED
    assert count(store.conn, "prs") == 0


def test_union_of_disjoint_labels_is_visible_before_any_removal(store: Store) -> None:
    with store.transaction() as c:
        wid = pr_watch(c, "P1", "h/o/r#1", A, B)
        w.add_labels(c, wid, A, [L1])
        w.add_labels(c, wid, B, [L2, Label("z", "1")])
        assert w.labels_of(c, wid) == ["k=v", "x=y", "z=1"]
        assert w.labels_of(c, wid, A) == ["k=v"]
        assert w.labels_of(c, wid, B) == ["x=y", "z=1"]


def test_watch_view_lists_only_its_own_owners_and_labels(store: Store) -> None:
    with store.transaction() as c:
        w1 = pr_watch(c, "P1", "h/o/r#1", A)
        w2 = pr_watch(c, "P2", "h/o/r#2", B)
        w.add_labels(c, w1, A, [L1])
        w.add_labels(c, w2, B, [L2])
        v1, v2 = w.watch_view(c, w1), w.watch_view(c, w2)
    assert v1 is not None and v2 is not None
    assert (v1["owners"], v1["labels"]) == ([A], ["k=v"])
    assert (v2["owners"], v2["labels"]) == ([B], ["x=y"])


def test_add_source_reports_whether_it_created_the_row(store: Store) -> None:
    with store.transaction() as c:
        wid = pr_watch(c, "P1", "h/o/r#1", A)
        q, _ = w.ensure_watch(c, "query", "mine+open", NOW)
        assert w.add_source(c, "P1", q) is True
        assert w.add_source(c, "P1", q) is False
        assert w.add_source(c, "P1", wid) is False  # pr_watch already added it
