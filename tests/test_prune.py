"""`prune`: event retention, row expiry, lease and scope cleanup, through main()."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pr_watch import prune as prune_mod
from pr_watch import watches
from pr_watch.cli import main
from pr_watch.leases import Leases
from pr_watch.state_dir import open_state_dir
from pr_watch.store import NewEvent, Store, StoreError, format_ts, open_store
from tests.harness import assert_valid

GOLDEN = Path(__file__).parent / "golden" / "prune"
A, B = "human:a", "agent:b"
NOW = datetime(2026, 6, 1, tzinfo=UTC)
OLD = "2026-04-01T00:00:00.000000Z"  # far past the 30d retention and the 7d expiry
EV_CUTOFF = "2026-05-02T00:00:00.000000Z"  # NOW - 30d
EX_CUTOFF = "2026-05-25T00:00:00.000000Z"  # NOW - 7d
RECENT = "2026-05-30T00:00:00.000000Z"


@pytest.fixture(autouse=True)
def pinned_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prune_mod, "_now", lambda: NOW)


@pytest.fixture
def store(state_dir: Path) -> Iterator[Store]:
    with open_state_dir(state_dir) as state, open_store(state) as s:
        yield s


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(["prune", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def jrun(capsys: pytest.CaptureFixture[str], *argv: str) -> dict[str, Any]:
    code, out, err = run(capsys, "--json", *argv)
    assert code == 0, (out, err)
    d = json.loads(out)
    assert_valid("cli/prune", d)
    return d  # type: ignore[no-any-return]


def add_event(store: Store, at: str, pr_id: str = "P1") -> int:
    with store.transaction() as c:
        ev = store.append_event(
            NewEvent(
                "pushed",
                pr_id,
                "h/o/r#1",
                {"repo": "h/o/r", "occurred_at": None, "head_sha": "a" * 40, "watches": []},
                [],
            )
        )
        c.execute("UPDATE events SET detected_at = ? WHERE seq = ?", (at, ev.seq))
    return ev.seq


def add_pr(
    store: Store,
    pr_id: str,
    *,
    status: str = "terminal",
    terminal_at: str | None = OLD,
    per_pr_watch: bool = True,
    members: tuple[str, ...] = (),
    repo: str = "h/o/r",
    kind: str = "merged",
) -> None:
    with store.transaction() as c:
        c.execute(
            "INSERT INTO prs (pr_id, pr, repo, host, status, terminal_at, terminal_kind, digest) "
            "VALUES (?, ?, ?, 'h', ?, ?, ?, '{}')",
            (pr_id, f"{repo}#{abs(hash(pr_id)) % 9000 + 1}", repo, status, terminal_at,
             kind if status == "terminal" else None),
        )  # fmt: skip
        if per_pr_watch:
            wid, _ = watches.ensure_watch(c, "pr", pr_id, NOW)
            watches.add_source(c, pr_id, wid)
            watches.add_owner(c, wid, A)
            watches.add_owner(c, wid, B)
            c.execute("INSERT INTO labels (watch_id, owner, label) VALUES (?, ?, 'k=v')", (wid, A))
        for q in members:
            qid, _ = watches.ensure_watch(c, "query", q, NOW)
            watches.add_owner(c, qid, A)
            watches.add_source(c, pr_id, qid)


def add_lease(
    store: Store, pr_id: str, *, boot: str | None = None, expires: str | None = None
) -> None:
    me = Leases(store, ttl=timedelta(minutes=6)).identity
    with store.transaction() as c:
        c.execute(
            "INSERT INTO leases (pr_id, holder_pid, holder_start, boot_id, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (pr_id, me.pid, me.start, boot or me.boot_id, expires or "2026-06-01T00:05:00.000000Z"),
        )


def table(store: Store, name: str) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in store.conn.execute(f"SELECT * FROM {name} ORDER BY 1")]  # noqa: S608


def meta(store: Store, key: str) -> str | None:
    row = store.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def count(store: Store, name: str) -> int:
    return int(store.conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0])  # noqa: S608


# --- shape -----------------------------------------------------------------------------------


def test_empty_store_prunes_nothing(capsys: pytest.CaptureFixture[str], store: Store) -> None:
    d = jrun(capsys)
    assert d == {
        "dry_run": False, "events_deleted": 0, "prs_deleted": 0,
        "watches_ended": 0, "members_dropped": 0, "leases_deleted": 0,
    }  # fmt: skip


def test_golden_json(capsys: pytest.CaptureFixture[str], store: Store) -> None:
    add_event(store, OLD)
    add_pr(store, "P1", members=("q1",))
    add_lease(store, "P1")
    d = jrun(capsys)
    path = GOLDEN / "full.json"
    if not path.exists():
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n")
        pytest.fail("golden written; review it")
    assert d == json.loads(path.read_text())


def test_human_output_and_quiet(capsys: pytest.CaptureFixture[str], store: Store) -> None:
    add_event(store, OLD)
    code, out, _ = run(capsys)
    assert code == 0 and "1 event" in out
    code, out, _ = run(capsys, "--quiet")
    assert code == 0 and out == ""


def test_human_output_pluralises_every_noun(
    capsys: pytest.CaptureFixture[str], store: Store
) -> None:
    add_event(store, OLD)
    add_event(store, OLD)
    add_pr(store, "P1", members=("q1", "q2"))
    add_pr(store, "P2")
    code, out, _ = run(capsys)
    assert code == 0
    assert out.strip() == (
        "deleted: 2 events, 2 PRs (2 watches ended, 2 members dropped), 0 leases"
    )
    add_event(store, OLD)
    add_pr(store, "P3", per_pr_watch=True)
    code, out, _ = run(capsys)
    assert out.strip() == "deleted: 1 event, 1 PR (1 watch ended, 0 members dropped), 0 leases"


def test_dry_run_human_says_would(capsys: pytest.CaptureFixture[str], store: Store) -> None:
    add_event(store, OLD)
    code, out, _ = run(capsys, "--dry-run")
    assert code == 0 and "would" in out.lower()


# --- events ----------------------------------------------------------------------------------


def test_events_older_than_retention_are_deleted_newer_kept(
    capsys: pytest.CaptureFixture[str], store: Store
) -> None:
    old = add_event(store, OLD)
    edge = add_event(store, EV_CUTOFF)  # exactly the cutoff is not older
    new = add_event(store, RECENT)
    d = jrun(capsys)
    assert d["events_deleted"] == 1
    assert [r[0] for r in table(store, "events")] == [edge, new]
    assert meta(store, "pruned_through_seq") == str(old)


def test_head_survives_and_seq_is_never_reused(
    capsys: pytest.CaptureFixture[str], store: Store
) -> None:
    add_event(store, OLD)
    last = add_event(store, OLD)
    jrun(capsys)
    assert count(store, "events") == 0
    assert store.head_seq() == last
    assert add_event(store, RECENT) == last + 1


def test_pruned_through_seq_never_goes_back(
    capsys: pytest.CaptureFixture[str], store: Store
) -> None:
    add_event(store, OLD)
    with store.transaction() as c:
        c.execute("UPDATE meta SET value = '100' WHERE key = 'pruned_through_seq'")
    jrun(capsys)
    assert meta(store, "pruned_through_seq") == "100"


def test_a_clock_stepped_back_keeps_every_event(
    capsys: pytest.CaptureFixture[str], store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_event(store, RECENT)
    add_pr(store, "P1", terminal_at=RECENT)
    monkeypatch.setattr(prune_mod, "_now", lambda: datetime(2020, 1, 1, tzinfo=UTC))
    d = jrun(capsys)
    assert d["events_deleted"] == 0 and d["prs_deleted"] == 0
    assert count(store, "events") == 1 and count(store, "prs") == 1


def test_out_of_order_detected_at_deletes_only_the_old_ones(
    capsys: pytest.CaptureFixture[str], store: Store
) -> None:
    first = add_event(store, RECENT)  # a lower seq with a newer stamp (clock stepped back once)
    second = add_event(store, OLD)
    jrun(capsys)
    assert [r[0] for r in table(store, "events")] == [first]
    assert meta(store, "pruned_through_seq") == str(second)


def test_events_go_in_bounded_batches(
    capsys: pytest.CaptureFixture[str], store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prune_mod, "EVENT_BATCH", 2)
    for _ in range(5):
        add_event(store, OLD)
    d = jrun(capsys)
    assert d["events_deleted"] == 5 and count(store, "events") == 0
    assert meta(store, "pruned_through_seq") == "5"


def test_batches_are_separate_transactions(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prune_mod, "EVENT_BATCH", 2)
    for _ in range(5):
        add_event(store, OLD)
    seen: list[bool] = []
    real = prune_mod._delete_event_batch

    def spy(s: Store, cutoff: str) -> int:
        seen.append(s.conn.in_transaction)
        return real(s, cutoff)

    monkeypatch.setattr(prune_mod, "_delete_event_batch", spy)
    prune_mod.prune(store, NOW, "30d", "7d")
    assert seen == [False, False, False]  # 2+2+1, none nested inside a transaction


def test_a_stop_flag_ends_prune_between_batches_and_stamps_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prune_mod, "EVENT_BATCH", 2)
    for _ in range(7):
        add_event(store, OLD)
    batches: list[int] = []
    real = prune_mod._delete_event_batch

    def counted(s: Store, cutoff: str) -> int:
        batches.append(1)
        return real(s, cutoff)

    monkeypatch.setattr(prune_mod, "_delete_event_batch", counted)
    result = prune_mod.prune(store, NOW, "30d", "7d", should_stop=lambda: len(batches) >= 1)
    assert len(batches) == 1 and result.events_deleted == 2  # stopped after the first batch
    assert count(store, "events") == 5 and meta(store, "last_prune_at") is None
    assert meta(store, "pruned_through_seq") == "2"  # what was deleted is consistent


def test_a_stop_flag_set_before_prune_starts_does_nothing(store: Store) -> None:
    add_event(store, OLD)
    result = prune_mod.prune(store, NOW, "30d", "7d", should_stop=lambda: True)
    assert result == prune_mod.PruneResult() and count(store, "events") == 1
    assert meta(store, "last_prune_at") is None


def test_an_event_appended_between_batches_is_never_lost(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prune_mod, "EVENT_BATCH", 2)
    for _ in range(4):
        add_event(store, OLD)
    real = prune_mod._delete_event_batch
    added: list[int] = []

    def interleave(s: Store, cutoff: str) -> int:
        n = real(s, cutoff)
        if not added:
            added.append(add_event(s, format_ts(NOW)))  # a poll commit between two batches
        return n

    monkeypatch.setattr(prune_mod, "_delete_event_batch", interleave)
    result = prune_mod.prune(store, NOW, "30d", "7d")
    assert result.events_deleted == 4
    assert [r[0] for r in table(store, "events")] == added
    assert meta(store, "pruned_through_seq") == "4"  # below the new event: its cursor is no gap


def test_retention_comes_from_config(
    capsys: pytest.CaptureFixture[str], store: Store, tmp_path: Path
) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("retention:\n  events: 90d\n")
    cfg.chmod(0o600)
    add_event(store, OLD)  # 61 days old: inside 90d
    code = main(["--config", str(cfg), "--json", "prune"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["events_deleted"] == 0 and count(store, "events") == 1


# --- expiry ----------------------------------------------------------------------------------


def test_expired_terminal_rows_go_with_their_watches_and_members(
    capsys: pytest.CaptureFixture[str], store: Store
) -> None:
    add_pr(store, "P1", members=("q1", "q2"))
    d = jrun(capsys)
    assert (d["prs_deleted"], d["watches_ended"], d["members_dropped"]) == (1, 1, 2)
    assert count(store, "prs") == 0 and count(store, "sources") == 0
    assert count(store, "owners") == 2 and count(store, "labels") == 0  # only the query watches
    assert {r[2] for r in table(store, "watches")} == {"q1", "q2"}


def test_a_member_only_row_is_dropped_without_a_watch_ending(
    capsys: pytest.CaptureFixture[str], store: Store
) -> None:
    add_pr(store, "P1", per_pr_watch=False, members=("q1",))
    d = jrun(capsys)
    assert (d["prs_deleted"], d["watches_ended"], d["members_dropped"]) == (1, 0, 1)


def test_rows_that_do_not_satisfy_the_predicate_stay(
    capsys: pytest.CaptureFixture[str], store: Store
) -> None:
    add_pr(store, "AT_CUTOFF", terminal_at=EX_CUTOFF)
    add_pr(store, "FRESH", terminal_at=RECENT)
    add_pr(store, "NO_STAMP", terminal_at=None)
    add_pr(store, "ACTIVE", status="active", terminal_at=OLD)
    add_pr(store, "UNREACH", status="unreachable", terminal_at=OLD)
    d = jrun(capsys)
    assert d["prs_deleted"] == 0 and count(store, "prs") == 5 and count(store, "watches") == 5


def test_prune_and_watch_add_share_one_drop_implementation(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_pr(store, "P1", members=("q1",))
    add_pr(store, "P2", status="active")
    calls: list[tuple[str, str]] = []
    real = watches.drop_expired_row

    def spy(conn: sqlite3.Connection, pr_id: str, cutoff: str) -> watches.ExpiredDrop | None:
        calls.append((pr_id, cutoff))
        return real(conn, pr_id, cutoff)

    monkeypatch.setattr(watches, "drop_expired_row", spy)
    prune_mod.prune(store, NOW, "30d", "7d")
    assert calls == [("P1", EX_CUTOFF)]
    add_pr(store, "P3")
    with store.transaction() as c:
        assert watches.drop_expired_pr(c, "P3", EX_CUTOFF) is True
    assert calls[-1] == ("P3", EX_CUTOFF)


def test_drop_expired_row_rechecks_the_predicate(store: Store) -> None:
    add_pr(store, "P1")
    with store.transaction() as c:
        c.execute("UPDATE prs SET terminal_at = ? WHERE pr_id = 'P1'", (RECENT,))  # reopened
        assert watches.drop_expired_row(c, "P1", EX_CUTOFF) is None
    assert count(store, "prs") == 1


def test_expiry_clears_leases_and_scopes_of_the_dropped_rows(
    capsys: pytest.CaptureFixture[str], store: Store
) -> None:
    add_pr(store, "P1", repo="h/gone")
    add_pr(store, "P2", status="active", repo="h/kept")
    add_lease(store, "P1")
    add_lease(store, "P2")
    with store.transaction() as c:
        c.execute("INSERT INTO scopes (repo, watermark) VALUES ('h/gone', ?)", (OLD,))
        c.execute("INSERT INTO scopes (repo, watermark) VALUES ('h/kept', ?)", (OLD,))
    d = jrun(capsys)
    assert d["leases_deleted"] == 1
    assert [r[0] for r in table(store, "leases")] == ["P2"]
    assert [r[0] for r in table(store, "scopes")] == ["h/kept"]


def test_scopes_of_a_repo_with_no_row_are_orphans_and_go(
    capsys: pytest.CaptureFixture[str], store: Store
) -> None:
    with store.transaction() as c:
        c.execute("INSERT INTO scopes (repo, watermark) VALUES ('h/old', ?)", (OLD,))
    jrun(capsys)
    assert count(store, "scopes") == 0


def test_a_scope_stays_while_any_row_of_its_repo_remains(
    capsys: pytest.CaptureFixture[str], store: Store
) -> None:
    add_pr(store, "P1", status="unreachable", repo="h/r")
    with store.transaction() as c:
        c.execute("INSERT INTO scopes (repo, watermark) VALUES ('h/r', ?)", (OLD,))
    jrun(capsys)
    assert count(store, "scopes") == 1


# --- leases ----------------------------------------------------------------------------------


def test_only_expired_dead_and_orphaned_leases_go(
    capsys: pytest.CaptureFixture[str], store: Store
) -> None:
    for pr_id in ("LIVE", "EXPIRED", "DEAD"):
        add_pr(store, pr_id, status="active")
    add_lease(store, "LIVE")
    add_lease(store, "EXPIRED", expires="2026-05-31T00:00:00.000000Z")
    add_lease(store, "DEAD", boot="another-boot")
    add_lease(store, "ORPHAN")  # no row
    d = jrun(capsys)
    assert d["leases_deleted"] == 3
    assert [r[0] for r in table(store, "leases")] == ["LIVE"]


def test_a_lease_far_in_the_future_counts_as_expired(
    capsys: pytest.CaptureFixture[str], store: Store
) -> None:
    add_pr(store, "P1", status="active")
    add_lease(store, "P1", expires="2030-01-01T00:00:00.000000Z")
    assert jrun(capsys)["leases_deleted"] == 1


def test_hostile_identifiers_are_handled(capsys: pytest.CaptureFixture[str], store: Store) -> None:
    weird = "x'; DROP TABLE prs; --é‮"
    add_lease(store, weird)
    with store.transaction() as c:
        c.execute("INSERT INTO scopes (repo, watermark) VALUES (?, NULL)", (weird,))
    assert jrun(capsys)["leases_deleted"] == 1
    assert count(store, "scopes") == 0


# --- dry run ---------------------------------------------------------------------------------


def populate(store: Store) -> None:
    add_event(store, OLD)
    add_event(store, RECENT)
    add_pr(store, "P1", members=("q1",), repo="h/gone")
    add_pr(store, "P2", status="active")
    add_lease(store, "P1")
    add_lease(store, "P2", boot="another-boot")
    with store.transaction() as c:
        c.execute("INSERT INTO scopes (repo, watermark) VALUES ('h/gone', ?)", (OLD,))


def _shape_terminal_mixed(store: Store) -> None:
    add_pr(store, "P1", members=("q1", "q2"), repo="h/gone")
    add_pr(store, "P2", per_pr_watch=False, members=("q1",))
    add_pr(store, "P3", status="active")
    add_pr(store, "P4", terminal_at=RECENT)


def _shape_null_terminal_and_unreachable(store: Store) -> None:
    add_pr(store, "P1", status="unreachable", terminal_at=None)
    add_pr(store, "P2", status="active", terminal_at=OLD)  # active with a stale terminal_at
    add_pr(store, "P3", terminal_at=None)
    add_pr(store, "P4", terminal_at=OLD)


def _shape_leases(store: Store) -> None:
    add_pr(store, "P1", status="active")
    add_pr(store, "P2")  # expires this run: its lease is doomed with it
    add_lease(store, "P1")  # live
    add_lease(store, "P2")
    add_lease(store, "GHOST")  # no row
    add_lease(store, "P1b", boot="another-boot")  # dead boot, no row
    add_lease(store, "P3", expires="2030-01-01T00:00:00.000000Z")  # far future
    with store.transaction() as c:
        c.execute(
            "INSERT INTO prs (pr_id, pr, repo, host, status, digest) "
            "VALUES ('P3', 'h/o/r#3', 'h/o/r', 'h', 'active', '{}')"
        )


def _shape_scopes_and_events(store: Store) -> None:
    add_pr(store, "P1", repo="h/a")
    add_pr(store, "P2", status="active", repo="h/b")
    with store.transaction() as c:
        for repo in ("h/a", "h/b", "h/c"):
            c.execute("INSERT INTO scopes (repo, watermark) VALUES (?, ?)", (repo, OLD))
    add_event(store, OLD, "P1")
    add_event(store, EV_CUTOFF, "P2")  # exactly at the cutoff: kept
    add_event(store, RECENT, "P2")


SHAPES = {
    "terminal_mixed": _shape_terminal_mixed,
    "null_terminal_and_unreachable": _shape_null_terminal_and_unreachable,
    "leases": _shape_leases,
    "scopes_and_events": _shape_scopes_and_events,
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_dry_run_equals_a_real_run_for_every_state_shape(
    capsys: pytest.CaptureFixture[str], store: Store, shape: str
) -> None:
    SHAPES[shape](store)
    dry = jrun(capsys, "--dry-run")
    assert dry["dry_run"] is True
    assert jrun(capsys) == {**dry, "dry_run": False}


def test_dry_run_reports_what_a_real_run_deletes(
    capsys: pytest.CaptureFixture[str], store: Store
) -> None:
    populate(store)
    dry = jrun(capsys, "--dry-run")
    assert dry["dry_run"] is True and dry["events_deleted"] == 1 and dry["leases_deleted"] == 2
    real = jrun(capsys)
    assert real == {**dry, "dry_run": False}


def test_dry_run_writes_nothing(
    capsys: pytest.CaptureFixture[str], store: Store, state_dir: Path
) -> None:
    populate(store)
    store.conn.execute("PRAGMA wal_checkpoint(FULL)")
    before_tables = {
        t: table(store, t)
        for t in (
            "events",
            "prs",
            "watches",
            "owners",
            "labels",
            "sources",
            "leases",
            "scopes",
            "meta",
        )
    }
    files = sorted(state_dir.iterdir())

    def fingerprint() -> dict[str, tuple[int, int, str]]:
        out = {}
        for p in sorted(state_dir.iterdir()):
            if p.is_file():
                st = p.stat()
                out[p.name] = (
                    st.st_size,
                    st.st_mtime_ns,
                    hashlib.sha256(p.read_bytes()).hexdigest(),
                )
        return out

    before = fingerprint()
    code = main(["prune", "--dry-run", "--json"])
    capsys.readouterr()
    assert code == 0
    assert sorted(state_dir.iterdir()) == files
    after = fingerprint()
    # state.db and the log are untouched; -wal/-shm may be created empty by a read-only reader
    assert after["state.db"] == before["state.db"]
    for name in before:
        if name.endswith("-wal"):
            assert after[name] == before[name]
    after_tables = {t: table(store, t) for t in before_tables}
    assert after_tables == before_tables
    assert meta(store, "last_prune_at") is None


def test_dry_run_never_opens_the_store_for_writing(
    capsys: pytest.CaptureFixture[str], store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    populate(store)

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("dry-run opened the store for writing")

    monkeypatch.setattr("pr_watch.cli.open_store", boom)
    assert jrun(capsys, "--dry-run")["events_deleted"] == 1


def test_dry_run_on_a_missing_store_creates_nothing(
    capsys: pytest.CaptureFixture[str], state_dir: Path
) -> None:
    assert not state_dir.exists()
    d = jrun(capsys, "--dry-run")
    assert d["dry_run"] is True and d["events_deleted"] == 0
    assert not state_dir.exists()


def test_dry_run_leaves_no_log_record(
    capsys: pytest.CaptureFixture[str], store: Store, state_dir: Path
) -> None:
    populate(store)
    jrun(capsys, "--dry-run")
    assert not (state_dir / "log.jsonl").exists()


# --- housekeeping ----------------------------------------------------------------------------


def test_a_real_run_stamps_last_prune_at_and_checkpoints(store: Store, state_dir: Path) -> None:
    add_event(store, OLD)
    prune_mod.prune(store, NOW, "30d", "7d")
    assert meta(store, "last_prune_at") == format_ts(NOW)
    wal = state_dir / "state.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0


def test_a_reader_snapshot_does_not_make_the_checkpoint_block_writers(
    store: Store, state_dir: Path
) -> None:
    add_event(store, OLD)
    add_event(store, RECENT)
    reader = sqlite3.connect(state_dir / "state.db", isolation_level=None)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM events").fetchone()  # holds a WAL snapshot
        started = time.monotonic()
        result = prune_mod.prune(store, NOW, "30d", "7d")
        elapsed = time.monotonic() - started
        assert result.events_deleted == 1
        assert elapsed < 1.0  # the 5 s busy_timeout is not waited out
        assert int(store.conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 5000  # restored
        with store.transaction() as c:  # a writer is not held up either
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('probe', '1')")
    finally:
        reader.close()


def test_a_second_run_deletes_nothing(capsys: pytest.CaptureFixture[str], store: Store) -> None:
    populate(store)
    jrun(capsys)
    d = jrun(capsys)
    assert (d["events_deleted"], d["prs_deleted"], d["leases_deleted"]) == (0, 0, 0)


def test_prune_on_a_database_is_mode_0600(
    capsys: pytest.CaptureFixture[str], state_dir: Path
) -> None:
    jrun(capsys)
    assert (state_dir / "state.db").stat().st_mode & 0o777 == 0o600
    assert os.access(state_dir, os.W_OK)


def test_sqlite_errors_are_not_swallowed(store: Store) -> None:
    with store.transaction():
        with pytest.raises(StoreError):  # a nested transaction is refused
            prune_mod.prune(store, NOW, "30d", "7d")
    assert isinstance(store.conn, sqlite3.Connection)
