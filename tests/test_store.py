"""The SQLite store: safe open, migrations, store_id, transactions and event append."""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from pr_watch import store as store_module
from pr_watch.state_dir import StateDir, StateDirError, open_state_dir
from pr_watch.store import (
    EVENT_ID_RE,
    EVENT_TYPES,
    MIGRATIONS,
    SCHEMA_VERSION,
    STORE_ID_RE,
    EventError,
    NewEvent,
    Store,
    StoreCorruptError,
    StoreError,
    StoreTooNewError,
    open_store,
)
from tests.harness import assert_valid

PR = "github.com/o/r#1"
PR_ID = "PR_abc123"
SHA = "a" * 40
TS = "2026-01-02T03:04:05.000006Z"
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


def db_path(state: StateDir) -> Path:
    return state.path / "state.db"


def closed_event(**changes: Any) -> NewEvent:
    base: dict[str, Any] = {"type": "closed", "pr_id": PR_ID, "pr": PR, "payload": dict(COMMON)}
    base.update(changes)
    return NewEvent(**base)


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


# --- creation and schema ---------------------------------------------------------------------


def test_creates_a_private_wal_database_with_the_v1_schema(state: StateDir) -> None:
    with open_store(state) as s:
        path = db_path(state)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert s.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert s.schema_version == SCHEMA_VERSION == 1
        assert table_names(s.conn) >= {
            "meta", "hosts", "scopes", "watches", "owners", "labels", "prs", "sources",
            "leases", "events",
        }  # fmt: skip
        assert s.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert s.conn.execute("PRAGMA user_version").fetchone()[0] == 1
        meta = dict(s.conn.execute("SELECT key, value FROM meta").fetchall())
        assert meta["schema_version"] == "1"
        assert meta["pruned_through_seq"] == "0"
        assert STORE_ID_RE.match(meta["store_id"]) and meta["store_id"] == s.store_id
        for suffix in ("-wal", "-shm"):
            side = Path(str(path) + suffix)
            if side.exists():
                assert stat.S_IMODE(side.stat().st_mode) == 0o600


def test_mode_is_0600_under_a_permissive_umask(tmp_path: Path) -> None:
    old = os.umask(0)
    try:
        with open_state_dir(tmp_path / "pr-watch") as st, open_store(st):
            assert stat.S_IMODE(db_path(st).stat().st_mode) == 0o600
    finally:
        os.umask(old)


def test_reopen_keeps_store_id_and_data(state: StateDir) -> None:
    with open_store(state) as first:
        sid = first.store_id
        with first.transaction():
            first.append_event(closed_event())
    with open_store(state) as second:
        assert second.store_id == sid
        assert second.head_seq() == 1


def test_a_new_database_gets_a_new_store_id(tmp_path: Path) -> None:
    ids = set()
    for name in ("a", "b", "c"):
        with open_state_dir(tmp_path / name) as st, open_store(st) as s:
            ids.add(s.store_id)
    assert len(ids) == 3


def test_busy_timeout_is_set(state: StateDir) -> None:
    with open_store(state, busy_timeout_ms=1234) as s:
        assert s.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1234


def test_cascades_and_foreign_keys_work(store: Store) -> None:
    c = store.conn
    c.execute("INSERT INTO watches VALUES ('w-aaaaaaaaaa', 'pr', ?, ?)", (PR_ID, TS))
    c.execute("INSERT INTO owners VALUES ('w-aaaaaaaaaa', 'me')")
    c.execute("INSERT INTO labels VALUES ('w-aaaaaaaaaa', 'me', 'k=v')")
    c.execute("INSERT INTO prs (pr_id, pr, repo, host) VALUES (?, ?, 'r', 'h')", (PR_ID, PR))
    c.execute("INSERT INTO sources (pr_id, watch_id) VALUES (?, 'w-aaaaaaaaaa')", (PR_ID,))
    with pytest.raises(sqlite3.IntegrityError):  # label without its owner
        c.execute("INSERT INTO labels VALUES ('w-aaaaaaaaaa', 'other', 'k=v')")
    with pytest.raises(sqlite3.IntegrityError):  # left_count out of range
        c.execute("UPDATE sources SET left_count = 3")
    c.execute("DELETE FROM watches")
    for table in ("owners", "labels", "sources"):
        assert c.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


# --- migrations ------------------------------------------------------------------------------


def _v2(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE hosts ADD COLUMN note TEXT")


def test_upgrade_from_v1_to_v2_is_additive_and_keeps_data(state: StateDir) -> None:
    with open_store(state) as s:
        sid = s.store_id
        with s.transaction():
            s.append_event(closed_event())
            s.conn.execute("INSERT INTO hosts (host) VALUES ('h')")
    with open_store(state, migrations=(*MIGRATIONS, _v2)) as s:
        assert s.schema_version == 2
        assert s.store_id == sid
        assert s.head_seq() == 1
        assert s.conn.execute("SELECT host, note FROM hosts").fetchall() == [("h", None)]
        assert s.conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_fresh_database_runs_every_migration_once(state: StateDir) -> None:
    with open_store(state, migrations=(*MIGRATIONS, _v2)) as s:
        assert s.schema_version == 2
        cols = [r[1] for r in s.conn.execute("PRAGMA table_info(hosts)")]
        assert "note" in cols


def test_the_older_binary_refuses_a_newer_database_without_writing(state: StateDir) -> None:
    with open_store(state, migrations=(*MIGRATIONS, _v2)):
        pass
    path = db_path(state)
    before = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    with pytest.raises(StoreTooNewError) as info:
        open_store(state)
    assert info.value.code == "store_too_new"
    assert (info.value.found, info.value.supported) == (2, 1)
    assert "2" in str(info.value) and "1" in str(info.value)
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime


def test_newer_refusal_precedes_the_wal_switch(state: StateDir) -> None:
    # A rollback-journal database claiming a future version must stay in that mode.
    raw = sqlite3.connect(db_path(state))
    raw.execute("PRAGMA user_version = 99")
    raw.execute("CREATE TABLE t (x)")
    raw.commit()
    raw.close()
    with pytest.raises(StoreTooNewError):
        open_store(state)
    raw = sqlite3.connect(db_path(state))
    assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    raw.close()


def test_a_failing_migration_rolls_back_everything_on_a_new_file(state: StateDir) -> None:
    def boom(conn: sqlite3.Connection) -> None:
        conn.execute("ALTER TABLE hosts ADD COLUMN half TEXT")
        raise RuntimeError("mid-migration")

    with pytest.raises(RuntimeError):
        open_store(state, migrations=(*MIGRATIONS, boom))
    # The v1 step committed with its own transaction only if all steps did: nothing remains.
    raw = sqlite3.connect(db_path(state))
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 0
    assert raw.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    raw.close()
    with open_store(state) as s:  # and a later normal open succeeds
        assert s.schema_version == 1


def test_a_failing_upgrade_leaves_the_previous_version_intact(state: StateDir) -> None:
    with open_store(state):
        pass

    def boom(conn: sqlite3.Connection) -> None:
        conn.execute("ALTER TABLE hosts ADD COLUMN half TEXT")
        raise RuntimeError("mid-migration")

    with pytest.raises(RuntimeError):
        open_store(state, migrations=(*MIGRATIONS, boom))
    with open_store(state) as s:
        assert s.schema_version == 1
        assert "half" not in [r[1] for r in s.conn.execute("PRAGMA table_info(hosts)")]


def run_child(code: str, *args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ},
        check=False,
    )


def test_a_process_dying_mid_migration_leaves_a_consistent_database(state: StateDir) -> None:
    code = """
        import os, sys
        from pathlib import Path
        from pr_watch.state_dir import open_state_dir
        from pr_watch.store import open_store, MIGRATIONS
        def dies(conn):
            conn.execute("ALTER TABLE hosts ADD COLUMN half TEXT")
            os._exit(9)
        st = open_state_dir(Path(sys.argv[1]))
        open_store(st, migrations=(*MIGRATIONS, dies))
    """
    with open_store(state):
        pass
    done = run_child(code, str(state.path))
    assert done.returncode == 9
    with open_store(state) as s:
        assert s.schema_version == 1
        assert "half" not in [r[1] for r in s.conn.execute("PRAGMA table_info(hosts)")]


def test_a_process_dying_mid_event_append_leaves_no_partial_row(state: StateDir) -> None:
    code = """
        import os, sys
        from pathlib import Path
        from pr_watch.state_dir import open_state_dir
        from pr_watch.store import open_store, NewEvent
        st = open_state_dir(Path(sys.argv[1]))
        s = open_store(st)
        with s.transaction():
            s.append_event(NewEvent("closed", "PR_abc123", "github.com/o/r#1",
                {"repo": "github.com/o/r", "occurred_at": None, "head_sha": "a" * 40,
                 "watches": []}))
            os._exit(9)
    """
    with open_store(state) as s:
        with s.transaction():
            s.append_event(closed_event())
    done = run_child(code, str(state.path))
    assert done.returncode == 9
    with open_store(state) as s:
        assert s.conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert s.head_seq() == 1


def test_two_processes_creating_the_database_migrate_once(tmp_path: Path) -> None:
    target = tmp_path / "pr-watch"
    open_state_dir(target).close()
    code = """
        import sys, time
        from pathlib import Path
        from pr_watch.state_dir import open_state_dir
        from pr_watch.store import open_store
        go = float(sys.argv[2])
        while time.time() < go:
            time.sleep(0.001)
        with open_state_dir(Path(sys.argv[1])) as st, open_store(st) as s:
            print(s.store_id)
    """
    import time

    go = str(time.time() + 1.0)
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(code), str(target), go],
            stdout=subprocess.PIPE,
            text=True,
        )
        for _ in range(3)
    ]
    outs = [p.communicate(timeout=60)[0].strip() for p in procs]
    assert all(p.returncode == 0 for p in procs)
    assert len(set(outs)) == 1 and STORE_ID_RE.match(outs[0])  # one creation, one store_id
    with open_state_dir(target) as st, open_store(st) as s:
        assert s.store_id == outs[0]
        assert s.conn.execute("SELECT count(*) FROM meta WHERE key='store_id'").fetchone()[0] == 1


# --- hostile files ---------------------------------------------------------------------------


def test_a_non_sqlite_file_is_corrupt(state: StateDir) -> None:
    p = db_path(state)
    p.write_bytes(b"this is not a database at all, " * 20)
    p.chmod(0o600)
    with pytest.raises(StoreCorruptError) as info:
        open_store(state)
    assert info.value.code == "internal"
    assert "not a usable database" in str(info.value)
    assert p.read_bytes().startswith(b"this is not")  # untouched


def test_a_foreign_sqlite_database_is_refused_not_adopted(state: StateDir) -> None:
    raw = sqlite3.connect(db_path(state))
    raw.execute("CREATE TABLE somebody_elses (x)")
    raw.commit()
    raw.close()
    db_path(state).chmod(0o600)
    with pytest.raises(StoreCorruptError):
        open_store(state)
    raw = sqlite3.connect(db_path(state))
    assert table_names(raw) == {"somebody_elses"}
    raw.close()


def test_inconsistent_versions_are_corrupt(state: StateDir) -> None:
    with open_store(state):
        pass
    raw = sqlite3.connect(db_path(state))
    raw.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
    raw.commit()
    raw.close()
    with pytest.raises(StoreCorruptError):
        open_store(state)


def test_a_malformed_store_id_is_corrupt(state: StateDir) -> None:
    with open_store(state):
        pass
    raw = sqlite3.connect(db_path(state))
    raw.execute("UPDATE meta SET value = 'NOPE' WHERE key = 'store_id'")
    raw.commit()
    raw.close()
    with pytest.raises(StoreCorruptError):
        open_store(state)


def test_a_symlinked_database_is_refused(state: StateDir, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.db"
    target.write_bytes(b"")
    target.chmod(0o600)
    db_path(state).symlink_to(target)
    with pytest.raises(StateDirError, match="symlink"):
        open_store(state)
    assert target.read_bytes() == b""


@pytest.mark.parametrize("mode", [0o660, 0o606, 0o666])
def test_a_group_or_world_writable_database_is_refused(state: StateDir, mode: int) -> None:
    with open_store(state):
        pass
    db_path(state).chmod(mode)
    with pytest.raises(StateDirError, match="writable"):
        open_store(state)


def test_a_group_readable_database_is_accepted(state: StateDir) -> None:
    with open_store(state):
        pass
    db_path(state).chmod(0o640)
    with open_store(state):
        pass


def test_a_directory_in_place_of_the_database_is_refused(state: StateDir) -> None:
    db_path(state).mkdir()
    with pytest.raises(StateDirError):
        open_store(state)


def test_a_foreign_owned_database_is_refused(
    state: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open_store(state):
        pass
    from pr_watch import state_dir as sd

    monkeypatch.setattr(sd, "_geteuid", lambda: os.geteuid() + 1)
    with pytest.raises(StateDirError, match="another user"):
        open_store(state)


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_a_symlinked_sidecar_is_refused(state: StateDir, tmp_path: Path, suffix: str) -> None:
    with open_store(state):
        pass
    side = Path(str(db_path(state)) + suffix)
    if side.exists():
        side.unlink()
    target = tmp_path / "other"
    target.write_bytes(b"")
    side.symlink_to(target)
    with pytest.raises(StateDirError, match="symlink"):
        open_store(state)


def test_a_file_swapped_after_the_check_is_refused(
    state: StateDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open_store(state):
        pass
    real = store_module._connect

    def swapping(path: Path, busy_ms: int) -> sqlite3.Connection:
        conn = real(path, busy_ms)
        path.rename(path.with_name("old.db"))
        path.write_bytes(b"")
        path.chmod(0o600)
        return conn

    monkeypatch.setattr(store_module, "_connect", swapping)
    with pytest.raises(StateDirError, match="changed"):
        open_store(state)


def test_busy_database_reports_busy_not_a_traceback(state: StateDir) -> None:
    with open_store(state) as first:
        with first.transaction():
            with pytest.raises(StoreError) as info:
                open_store(state, busy_timeout_ms=50, migrations=(*MIGRATIONS, _v2))
    assert info.value.code == "internal"
    assert "busy" in str(info.value)


# --- transactions ----------------------------------------------------------------------------


def test_transaction_commits_and_rolls_back(store: Store) -> None:
    with store.transaction() as conn:
        conn.execute("INSERT INTO scopes (repo) VALUES ('a')")
    with pytest.raises(RuntimeError):
        with store.transaction() as conn:
            conn.execute("INSERT INTO scopes (repo) VALUES ('b')")
            raise RuntimeError("x")
    assert store.conn.execute("SELECT repo FROM scopes").fetchall() == [("a",)]
    assert not store.conn.in_transaction


def test_transactions_do_not_nest(store: Store) -> None:
    with store.transaction():
        with pytest.raises(StoreError, match="already open"):
            with store.transaction():
                pass
    assert not store.conn.in_transaction


def test_a_second_writer_waits_for_the_first(state: StateDir) -> None:
    with open_store(state) as a, open_store(state, busy_timeout_ms=50) as b:
        with a.transaction():
            with pytest.raises(StoreError, match="busy"):
                with b.transaction():
                    pass
        with b.transaction():
            pass


# --- events ----------------------------------------------------------------------------------


def test_append_assigns_increasing_seq_ids_and_returns_the_row(store: Store) -> None:
    when = datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=UTC)
    with store.transaction():
        a = store.append_event(closed_event(), when)
        b = store.append_event(closed_event(labels=["team=x"]), when)
    assert (a.seq, b.seq) == (1, 2)
    assert a.id != b.id
    assert EVENT_ID_RE.match(a.id) and EVENT_ID_RE.match(b.id)
    assert a.detected_at == TS
    assert store.head_seq() == 2
    assert b.labels == ["team=x"]
    row = store.conn.execute("SELECT id, type, pr_id, pr, detected_at, labels FROM events "
                             "WHERE seq = 2").fetchone()  # fmt: skip
    assert row == (b.id, "closed", PR_ID, PR, TS, '["team=x"]')


def test_stored_event_matches_the_event_schema(store: Store) -> None:
    with store.transaction():
        e = store.append_event(
            NewEvent(
                "pushed",
                PR_ID,
                PR,
                {**COMMON, "from_sha": SHA, "to_sha": "b" * 40, "forced": None},
                ["a=b"],
            )  # fmt: skip
        )
    assert_valid("event", e.to_json())
    with store.transaction():
        c = store.append_event(closed_event())
    assert_valid("event", c.to_json())


def test_detected_at_defaults_to_now_and_is_utc_microseconds(store: Store) -> None:
    with store.transaction():
        e = store.append_event(closed_event())
    assert store_module.TS_RE.match(e.detected_at)
    assert abs(datetime.fromisoformat(e.detected_at) - datetime.now(UTC)) < timedelta(minutes=1)


def test_a_non_utc_timestamp_is_converted(store: Store) -> None:
    tz = timezone(timedelta(hours=2))
    with store.transaction():
        e = store.append_event(closed_event(), datetime(2026, 1, 2, 5, 4, 5, 6, tzinfo=tz))
    assert e.detected_at == TS


def test_a_naive_timestamp_is_refused(store: Store) -> None:
    with pytest.raises(ValueError):
        with store.transaction():
            store.append_event(closed_event(), datetime(2026, 1, 1))
    assert store.head_seq() == 0


def test_append_outside_a_transaction_is_refused(store: Store) -> None:
    with pytest.raises(StoreError, match="inside a transaction"):
        store.append_event(closed_event())
    assert store.conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_a_rolled_back_transaction_stores_no_event_and_keeps_numbering_gapless(
    store: Store,
) -> None:
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.append_event(closed_event())
            raise RuntimeError("commit lost")
    with store.transaction():
        e = store.append_event(closed_event())
    assert e.seq == 1


def test_seq_is_never_reused_after_deletes(store: Store) -> None:
    with store.transaction() as conn:
        store.append_event(closed_event())
        last = store.append_event(closed_event())
        conn.execute("DELETE FROM events")
    assert store.head_seq() == last.seq == 2
    with store.transaction():
        assert store.append_event(closed_event()).seq == 3


def test_every_event_type_is_accepted(store: Store) -> None:
    with store.transaction():
        for t in sorted(EVENT_TYPES):
            store.append_event(closed_event(type=t))


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "bogus"},
        {"type": ""},
        {"pr_id": ""},
        {"pr_id": "a..b"},
        {"pr_id": "x" * 129},
        {"pr_id": "-lead"},
        {"pr": "not a pr"},
        {"pr": PR + "\n"},
        {"pr_id": PR_ID + "\n"},
        {"labels": ["bad label"]},
        {"labels": ["a=b", "a=b"]},
        {"labels": [f"k=v{i}" for i in range(300)]},
        {"labels": ["a=b\n"]},
        {"payload": {"id": "ev-aaaaaaaaaaaa"}},
        {"payload": {"seq": 1}},
        {"payload": {"labels": []}},
        {"payload": {"x": "y" * 5000}},
        {"payload": {"x": "y" * 4096, "z": ["y" * 4096] * 40}},
        {"payload": {"x": float("nan")}},
        {"payload": {"x": object()}},
        {"payload": {"x": {1: 2}}},
        {"payload": {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}},
        {"payload": {"k" * 65: 1}},
    ],
    ids=lambda c: str(sorted(c))[:30] + str(hash(str(c)) % 1000),
)
def test_malformed_events_are_rejected_and_store_nothing(
    store: Store, changes: dict[str, Any]
) -> None:
    with pytest.raises(EventError):
        with store.transaction():
            store.append_event(closed_event(**changes))
    assert store.conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_hostile_text_is_stored_escaped_and_round_trips(store: Store) -> None:
    nasty = "'); DROP TABLE events;-- ‮\x00\x1b[31m \U0001f600 é"
    with store.transaction():
        e = store.append_event(closed_event(payload={**COMMON, "note": nasty}))
    (raw,) = store.conn.execute("SELECT payload FROM events WHERE seq = ?", (e.seq,)).fetchone()
    assert raw.isascii() and "\x00" not in raw and "\x1b" not in raw
    assert store.conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    import json

    assert json.loads(raw)["note"] == nasty


def test_an_id_collision_draws_another_id(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = iter(["ev-aaaaaaaaaaaa", "ev-aaaaaaaaaaaa", "ev-bbbbbbbbbbbb"])
    monkeypatch.setattr(store_module, "new_event_id", lambda: next(ids))
    with store.transaction():
        a = store.append_event(closed_event())
        b = store.append_event(closed_event())
    assert (a.id, b.id) == ("ev-aaaaaaaaaaaa", "ev-bbbbbbbbbbbb")
    assert (a.seq, b.seq) == (1, 2)  # the failed insert did not burn a stored row


def test_concurrent_appenders_get_unique_ordered_seqs(state: StateDir) -> None:
    with open_store(state):
        pass
    code = """
        import sys
        from pathlib import Path
        from pr_watch.state_dir import open_state_dir
        from pr_watch.store import open_store, NewEvent
        with open_state_dir(Path(sys.argv[1])) as st, open_store(st) as s:
            for _ in range(10):
                with s.transaction():
                    s.append_event(NewEvent("closed", "PR_abc123", "github.com/o/r#1",
                        {"repo": "github.com/o/r", "occurred_at": None,
                         "head_sha": "a" * 40, "watches": []}))
    """
    procs = [
        subprocess.Popen([sys.executable, "-c", textwrap.dedent(code), str(state.path)])
        for _ in range(3)
    ]
    for p in procs:
        p.wait(timeout=120)
    assert [p.returncode for p in procs] == [0, 0, 0]
    with open_store(state) as s:
        rows = s.conn.execute("SELECT seq, id FROM events ORDER BY seq").fetchall()
    assert [r[0] for r in rows] == list(range(1, 31))
    assert len({r[1] for r in rows}) == 30
