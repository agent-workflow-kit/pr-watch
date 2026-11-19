"""The SQLite store: safe open, migrations, store_id, transactions and event append."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import textwrap
import time
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
    # A rollback-journal pr-watch database claiming a future version must stay in that mode.
    with open_store(state):
        pass
    raw = sqlite3.connect(db_path(state))
    raw.execute("PRAGMA journal_mode = DELETE")
    raw.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    raw.execute("PRAGMA user_version = 99")
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


def test_a_second_writer_times_out_as_busy_while_the_first_holds_the_lock(
    state: StateDir,
) -> None:
    with open_store(state) as a, open_store(state, busy_timeout_ms=50) as b:
        with a.transaction():
            with pytest.raises(StoreError, match="busy"):
                with b.transaction():
                    pass
        with b.transaction():
            pass


HOLDER = """
    import sys, time
    from pathlib import Path
    from pr_watch.state_dir import open_state_dir
    from pr_watch.store import open_store
    with open_state_dir(Path(sys.argv[1])) as st, open_store(st) as s:
        with s.transaction():
            print("held", flush=True)
            time.sleep(float(sys.argv[2]))
"""


def start_holder(state: StateDir, seconds: float) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(HOLDER), str(state.path), str(seconds)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_a_second_writer_waits_for_the_first_then_succeeds(state: StateDir) -> None:
    with open_store(state):
        pass
    holder = start_holder(state, 0.4)
    with open_store(state, busy_timeout_ms=10_000) as b:
        began = time.monotonic()
        with b.transaction():
            b.append_event(closed_event())
        waited = time.monotonic() - began
        assert b.head_seq() == 1
    _, err = holder.communicate(timeout=30)
    assert holder.returncode == 0, err
    assert 0.1 < waited < 8


def test_opening_a_current_database_does_not_wait_for_the_write_lock(state: StateDir) -> None:
    with open_store(state):
        pass
    holder = start_holder(state, 1.5)
    began = time.monotonic()
    with open_store(state, busy_timeout_ms=10_000) as s:  # reads only: no lock needed
        assert s.head_seq() == 0
    assert time.monotonic() - began < 1.0
    _, err = holder.communicate(timeout=30)
    assert holder.returncode == 0, err


def test_every_connection_sets_the_journal_size_limit(store: Store) -> None:
    assert store.conn.execute("PRAGMA journal_size_limit").fetchone()[0] == 64 * 1024 * 1024
    assert store.conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert store.conn.execute("PRAGMA trusted_schema").fetchone()[0] == 0


# --- state.db locks survive open and close (no descriptor of ours on state.db) ----------------

TRY_LOCK = """
    import sqlite3, sys
    conn = sqlite3.connect(sys.argv[1], timeout=0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        sys.exit(7)
    sys.exit(0)
"""


def child_takes_write_lock(state: StateDir) -> int:
    return run_child(TRY_LOCK, str(db_path(state))).returncode


def test_a_second_open_in_the_same_process_does_not_drop_the_held_write_lock(
    state: StateDir,
) -> None:
    with open_store(state) as a:
        with a.transaction():
            assert child_takes_write_lock(state) == 7
            with open_store(state):  # opens and closes everything it touches on state.db
                pass
            assert child_takes_write_lock(state) == 7  # still held
            a.append_event(closed_event())
        assert child_takes_write_lock(state) == 0  # released by the commit
    with open_store(state) as c:
        assert c.head_seq() == 1


def test_open_child_open_close_commit_again_keeps_every_row(state: StateDir) -> None:
    with open_store(state) as a:
        with a.transaction():
            a.append_event(closed_event())
        done = run_child(
            """
            import sys
            from pathlib import Path
            from pr_watch.state_dir import open_state_dir
            from pr_watch.store import open_store
            with open_state_dir(Path(sys.argv[1])) as st, open_store(st):
                pass
            """,
            str(state.path),
        )
        assert done.returncode == 0, done.stderr
        with a.transaction():
            a.append_event(closed_event())
    with open_store(state) as third:
        assert third.head_seq() == 2
        assert third.conn.execute("SELECT count(*) FROM events").fetchone()[0] == 2


REOPEN_LOOP = """
    import sys, time
    from pathlib import Path
    from pr_watch.state_dir import open_state_dir
    from pr_watch.store import open_store, NewEvent
    go = float(sys.argv[2])
    while time.time() < go:
        time.sleep(0.001)
    for _ in range(int(sys.argv[3])):
        with open_state_dir(Path(sys.argv[1])) as st, open_store(st) as s:
            with s.transaction():
                s.append_event(NewEvent("closed", "PR_abc123", "github.com/o/r#1",
                    {"repo": "github.com/o/r", "occurred_at": None, "head_sha": "a" * 40,
                     "watches": []}))
"""


def test_reopening_in_a_loop_from_three_processes_loses_no_commit(state: StateDir) -> None:
    with open_store(state):
        pass
    iterations = 60
    go = str(time.time() + 1.0)
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                textwrap.dedent(REOPEN_LOOP),
                str(state.path),
                go,
                str(iterations),
            ],  # fmt: skip
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(3)
    ]
    results = [p.communicate(timeout=300) for p in procs]
    codes = [p.returncode for p in procs]
    detail = [r[1][-600:] for r in results]
    assert codes == [0, 0, 0], detail
    with open_store(state) as s:
        count = s.conn.execute("SELECT count(*) FROM events").fetchone()[0]
        assert count == 3 * iterations, (count, detail)
        assert s.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_the_process_holds_no_descriptor_on_the_database_files(state: StateDir) -> None:
    with open_store(state):
        pass
    real_open = os.open
    seen: list[str] = []

    def spy(path: Any, *args: Any, **kwargs: Any) -> int:
        seen.append(str(path))
        return real_open(path, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "open", spy)
        with open_store(state):
            pass
    assert not [n for n in seen if "state.db" in n], seen


# --- foreign and newer databases are refused without a write ----------------------------------


def snapshot_files(state: StateDir) -> dict[str, tuple[bytes, int]]:
    out = {}
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path(state)) + suffix)
        if p.exists():
            out[suffix] = (p.read_bytes(), p.stat().st_mtime_ns)
    return out


@pytest.mark.parametrize("version", [1, 7])
def test_a_foreign_database_with_a_user_version_is_refused_untouched(
    state: StateDir, version: int
) -> None:
    raw = sqlite3.connect(db_path(state))
    raw.execute("CREATE TABLE somebody_elses (x)")
    raw.execute(f"PRAGMA user_version = {version}")
    raw.commit()
    raw.close()
    db_path(state).chmod(0o600)
    before = snapshot_files(state)
    with pytest.raises(StoreCorruptError) as info:
        open_store(state)
    assert info.value.code == "internal"
    assert snapshot_files(state) == before
    raw = sqlite3.connect(db_path(state))
    assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert raw.execute("PRAGMA user_version").fetchone()[0] == version
    raw.close()


def test_a_user_version_with_no_tables_is_refused_untouched(state: StateDir) -> None:
    raw = sqlite3.connect(db_path(state))
    raw.execute("PRAGMA user_version = 7")
    raw.close()
    db_path(state).chmod(0o600)
    before = snapshot_files(state)
    with pytest.raises(StoreCorruptError):
        open_store(state)
    assert snapshot_files(state) == before


def test_a_higher_user_version_than_meta_is_store_too_new(state: StateDir) -> None:
    with open_store(state):
        pass
    raw = sqlite3.connect(db_path(state))
    raw.execute("PRAGMA user_version = 9")
    raw.close()
    with pytest.raises(StoreTooNewError) as info:
        open_store(state)
    assert info.value.found == 9


def test_a_meta_without_a_store_id_is_corrupt(state: StateDir) -> None:
    with open_store(state):
        pass
    raw = sqlite3.connect(db_path(state))
    raw.execute("DELETE FROM meta WHERE key = 'store_id'")
    raw.commit()
    raw.close()
    with pytest.raises(StoreCorruptError):
        open_store(state)


def test_a_newer_database_with_an_uncheckpointed_wal_is_left_byte_identical(
    state: StateDir,
) -> None:
    done = run_child(
        """
        import os, sys
        from pathlib import Path
        from pr_watch.state_dir import open_state_dir
        from pr_watch.store import open_store, MIGRATIONS
        def v2(conn):
            conn.execute("ALTER TABLE hosts ADD COLUMN note TEXT")
        st = open_state_dir(Path(sys.argv[1]))
        s = open_store(st, migrations=(*MIGRATIONS, v2))
        with s.transaction():
            s.conn.execute("INSERT INTO hosts (host) VALUES ('h')")
        os._exit(0)
        """,
        str(state.path),
    )
    assert done.returncode == 0, done.stderr
    before = snapshot_files(state)
    assert before["-wal"][0], "the child must leave an uncheckpointed WAL"
    with pytest.raises(StoreTooNewError):
        open_store(state)
    after = snapshot_files(state)
    # The db and the WAL (the data) are byte- and mtime-identical. -shm is only a volatile index
    # that any reader updates, so only its presence is checked.
    assert after[""] == before[""]
    assert after["-wal"] == before["-wal"]
    assert set(after) == set(before)


def test_an_empty_state_db_file_is_initialized(state: StateDir) -> None:
    db_path(state).write_bytes(b"")
    db_path(state).chmod(0o600)
    with open_store(state) as s:
        assert s.schema_version == 1
        assert STORE_ID_RE.match(s.store_id)


# --- the PR row goes with its last source -----------------------------------------------------


def add_pr(c: sqlite3.Connection, pr_id: str, watches: list[str]) -> None:
    c.execute("INSERT INTO prs (pr_id, pr, repo, host) VALUES (?, ?, 'r', 'h')", (pr_id, PR))
    for w in watches:
        c.execute("INSERT OR IGNORE INTO watches VALUES (?, 'query', ?, ?)", (w, w, TS))
        c.execute("INSERT INTO sources (pr_id, watch_id) VALUES (?, ?)", (pr_id, w))


def pr_ids(c: sqlite3.Connection) -> list[str]:
    return [r[0] for r in c.execute("SELECT pr_id FROM prs ORDER BY pr_id")]


def test_deleting_the_last_source_drops_the_pr_in_the_same_transaction(store: Store) -> None:
    with store.transaction() as c:
        add_pr(c, "P1", ["w-aaaaaaaaaa"])
        c.execute("DELETE FROM sources WHERE pr_id = 'P1'")
        assert pr_ids(c) == []  # visible inside the transaction already
    assert pr_ids(store.conn) == []


def test_deleting_a_non_last_source_keeps_the_pr(store: Store) -> None:
    with store.transaction() as c:
        add_pr(c, "P1", ["w-aaaaaaaaaa", "w-bbbbbbbbbb"])
        c.execute("DELETE FROM sources WHERE pr_id = 'P1' AND watch_id = 'w-aaaaaaaaaa'")
    assert pr_ids(store.conn) == ["P1"]
    with store.transaction() as c:
        c.execute("DELETE FROM sources WHERE pr_id = 'P1'")
    assert pr_ids(store.conn) == []


def test_moving_a_pr_between_watches_inserts_the_new_source_first(store: Store) -> None:
    with store.transaction() as c:
        add_pr(c, "P1", ["w-aaaaaaaaaa"])
        c.execute("INSERT OR IGNORE INTO watches VALUES ('w-bbbbbbbbbb', 'query', 'k', ?)", (TS,))
        c.execute("INSERT INTO sources (pr_id, watch_id) VALUES ('P1', 'w-bbbbbbbbbb')")
        c.execute("DELETE FROM sources WHERE pr_id = 'P1' AND watch_id = 'w-aaaaaaaaaa'")
    assert pr_ids(store.conn) == ["P1"]


def test_deleting_a_pr_cascades_its_sources(store: Store) -> None:
    with store.transaction() as c:
        add_pr(c, "P1", ["w-aaaaaaaaaa"])
        add_pr(c, "P2", ["w-aaaaaaaaaa"])
        c.execute("DELETE FROM prs WHERE pr_id = 'P1'")
    assert pr_ids(store.conn) == ["P2"]
    assert store.conn.execute("SELECT pr_id FROM sources").fetchall() == [("P2",)]


def test_deleting_a_watch_drops_prs_left_without_a_source(store: Store) -> None:
    with store.transaction() as c:
        add_pr(c, "P1", ["w-aaaaaaaaaa"])
        add_pr(c, "P2", ["w-aaaaaaaaaa", "w-bbbbbbbbbb"])
        add_pr(c, "P3", ["w-bbbbbbbbbb"])
        c.execute("DELETE FROM watches WHERE id = 'w-aaaaaaaaaa'")
    assert pr_ids(store.conn) == ["P2", "P3"]


# --- event append details ---------------------------------------------------------------------


def test_labels_given_as_a_generator_are_stored(store: Store) -> None:
    with store.transaction():
        e = store.append_event(closed_event(labels=(x for x in ["a=b", "c=d"])))
    assert e.labels == ["a=b", "c=d"]
    (raw,) = store.conn.execute("SELECT labels FROM events WHERE seq = ?", (e.seq,)).fetchone()
    assert raw == '["a=b","c=d"]'


def test_a_sqlite_failure_during_append_is_a_store_error(store: Store) -> None:
    with pytest.raises(StoreError) as info:
        with store.transaction() as c:
            c.execute("DROP TABLE events")
            store.append_event(closed_event())
    assert not isinstance(info.value.__cause__, sqlite3.Error) or info.value.__suppress_context__
    assert type(info.value) is StoreError


def test_only_an_event_id_collision_is_retried(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def draw() -> str:
        calls.append(1)
        return "ev-aaaaaaaaaaaa"

    monkeypatch.setattr(store_module, "new_event_id", draw)
    with pytest.raises(StoreError) as info:
        with store.transaction() as c:
            c.execute(
                "CREATE TRIGGER no_events BEFORE INSERT ON events "
                "BEGIN SELECT RAISE(ABORT, 'nope'); END"
            )
            store.append_event(closed_event())
    assert len(calls) == 1
    assert type(info.value) is StoreError


def test_the_pr_cap_matches_the_schema() -> None:
    defs = json.loads((Path(__file__).parents[1] / "schemas" / "defs.json").read_text())
    assert store_module.PR_MAX_CHARS == defs["$defs"]["pr"].get("maxLength", 512)


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


BAD_EVENTS: list[Any] = [
    pytest.param({"type": "bogus"}, id="unknown-type"),
    pytest.param({"type": ""}, id="empty-type"),
    pytest.param({"pr_id": ""}, id="empty-pr-id"),
    pytest.param({"pr_id": "a..b"}, id="pr-id-dotdot"),
    pytest.param({"pr_id": "x" * 129}, id="pr-id-too-long"),
    pytest.param({"pr_id": "-lead"}, id="pr-id-leading-dash"),
    pytest.param({"pr": "not a pr"}, id="pr-malformed"),
    pytest.param({"pr": PR + "\n"}, id="pr-trailing-newline"),
    pytest.param({"pr": "github.com/" + "a" * 600 + "#1"}, id="pr-too-long"),
    pytest.param({"pr_id": PR_ID + "\n"}, id="pr-id-trailing-newline"),
    pytest.param({"labels": ["bad label"]}, id="label-malformed"),
    pytest.param({"labels": ["a=b", "a=b"]}, id="labels-repeated"),
    pytest.param({"labels": [f"k=v{i}" for i in range(300)]}, id="labels-too-many"),
    pytest.param({"labels": ["a=b\n"]}, id="label-newline"),
    pytest.param({"payload": {**COMMON, "id": "ev-aaaaaaaaaaaa"}}, id="payload-repeats-id"),
    pytest.param({"payload": {**COMMON, "seq": 1}}, id="payload-repeats-seq"),
    pytest.param({"payload": {**COMMON, "labels": []}}, id="payload-repeats-labels"),
    pytest.param({"payload": {**COMMON, "x": "y" * 5000}}, id="payload-long-string"),
    pytest.param({"payload": {**COMMON, "x": "y" * 4096, "z": ["y" * 4096] * 40}}, id="too-large"),
    pytest.param({"payload": {**COMMON, "x": float("nan")}}, id="payload-nan"),
    pytest.param({"payload": {**COMMON, "x": object()}}, id="payload-not-json"),
    pytest.param({"payload": {**COMMON, "x": {1: 2}}}, id="payload-int-key"),
    pytest.param(
        {"payload": {**COMMON, "a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}},
        id="payload-too-deep",
    ),
    pytest.param({"payload": {**COMMON, "k" * 65: 1}}, id="payload-long-key"),
    pytest.param({"payload": {**COMMON, "x": 2**53 + 1}}, id="payload-int-too-big"),
    pytest.param({"payload": {**COMMON, "x": -(2**53) - 1}}, id="payload-int-too-small"),
    pytest.param({"payload": {**COMMON, "x": "bad \ud800 surrogate"}}, id="payload-surrogate"),
    pytest.param({"payload": {**COMMON, "bad\udfff": 1}}, id="payload-surrogate-key"),
    pytest.param({"payload": {k: v for k, v in COMMON.items() if k != "repo"}}, id="no-repo"),
    pytest.param(
        {"payload": {k: v for k, v in COMMON.items() if k != "occurred_at"}}, id="no-occurred-at"
    ),
    pytest.param(
        {"payload": {k: v for k, v in COMMON.items() if k != "head_sha"}}, id="no-head-sha"
    ),
    pytest.param({"payload": {k: v for k, v in COMMON.items() if k != "watches"}}, id="no-watches"),
    pytest.param({"payload": {**COMMON, "repo": "Not A Repo"}}, id="bad-repo"),
    pytest.param({"payload": {**COMMON, "occurred_at": "yesterday"}}, id="bad-occurred-at"),
    pytest.param({"payload": {**COMMON, "occurred_at": 5}}, id="occurred-at-not-string"),
    pytest.param({"payload": {**COMMON, "head_sha": "abc"}}, id="bad-head-sha"),
    pytest.param({"payload": {**COMMON, "watches": "w-aaaaaaaaaa"}}, id="watches-not-list"),
    pytest.param({"payload": {**COMMON, "watches": ["nope"]}}, id="bad-watch-id"),
    pytest.param(
        {"payload": {**COMMON, "watches": ["w-aaaaaaaaaa", "w-aaaaaaaaaa"]}}, id="watches-repeated"
    ),
    pytest.param({"payload": {**COMMON, "watches": [["x"]]}}, id="watches-unhashable-element"),
]


@pytest.mark.parametrize("changes", BAD_EVENTS)
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
