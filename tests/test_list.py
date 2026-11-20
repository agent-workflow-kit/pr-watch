"""`list`: watches, PRs and host state, offline and read-only, through main()."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pr_watch import listing
from pr_watch.cli import main
from pr_watch.state_dir import open_state_dir
from pr_watch.store import MIGRATIONS, Store, StoreError, open_existing_store, open_store
from pr_watch.watches import watch_view
from tests.harness import assert_valid

GOLDEN = Path(__file__).parent / "golden" / "list"
A, B = "human:a", "agent:b"
T0 = "2026-06-01T00:00:00.000000Z"
T1 = "2026-06-01T01:00:00.000000Z"
T2 = "2026-06-01T02:00:00.000000Z"


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


NOW = datetime(2026, 6, 2, 0, 0, tzinfo=UTC)  # 24h after T0, 22h after T2


@pytest.fixture(autouse=True)
def pinned_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """ci_stuck compares against the clock: every test sees the same pinned one."""
    monkeypatch.setattr(listing, "_now", lambda: NOW)


@pytest.fixture
def store(state_dir: Path) -> Iterator[Store]:
    with open_state_dir(state_dir) as state, open_store(state) as s:
        yield s


def digest(state: str = "open", ci: str = "passing", **extra: Any) -> str:
    d = {
        "state": state,
        "draft": False,
        "head_sha": "a" * 40,
        "ci": {"state": ci, "head_sha": "a" * 40, "hold": 0},
        "blocked_by": [],
        "baselined": {"ci": True, "reviews": True, "threads": True},
    }
    d.update(extra)
    return json.dumps(d)


def add_pr(
    store: Store,
    pr_id: str,
    pr: str,
    *,
    status: str = "active",
    dig: str | None = None,
    sources: tuple[str, ...] = (),
    **cols: Any,
) -> None:
    host = pr.split("/")[0]
    cols = {"last_polled_at": T1 if dig else None, **cols}
    names = ["pr_id", "pr", "repo", "host", "status", "digest", *cols]
    vals = [pr_id, pr, pr.split("#")[0], host, status, dig, *cols.values()]
    with store.transaction() as c:
        # the sources trigger drops rows without a source, so sources are added in the same txn
        for w in sources:
            c.execute(
                "INSERT OR IGNORE INTO watches (id, kind, key, created_at) VALUES "
                "(?, 'query', ?, ?)",
                (w, "mine+open;host=" + w, T0),
            )
            c.execute("INSERT OR IGNORE INTO owners (watch_id, owner) VALUES (?, ?)", (w, A))
        c.execute(
            f"INSERT INTO prs ({', '.join(names)}) VALUES ({', '.join('?' * len(vals))})",  # noqa: S608
            vals,
        )
        for w in sources:
            c.execute("INSERT INTO sources (pr_id, watch_id) VALUES (?, ?)", (pr_id, w))


def add_watch(
    store: Store,
    wid: str,
    kind: str,
    key: str,
    owners: dict[str, list[str]],
    created: str = T0,
) -> None:
    with store.transaction() as c:
        c.execute(
            "INSERT INTO watches (id, kind, key, created_at) VALUES (?, ?, ?, ?)",
            (wid, kind, key, created),
        )
        for owner, labels in owners.items():
            c.execute("INSERT INTO owners (watch_id, owner) VALUES (?, ?)", (wid, owner))
            for label in labels:
                c.execute(
                    "INSERT INTO labels (watch_id, owner, label) VALUES (?, ?, ?)",
                    (wid, owner, label),
                )


def link(store: Store, pr_id: str, wid: str) -> None:
    with store.transaction() as c:
        c.execute("INSERT INTO sources (pr_id, watch_id) VALUES (?, ?)", (pr_id, wid))


def set_host(store: Store, host: str, **cols: str | None) -> None:
    with store.transaction() as c:
        c.execute("INSERT OR IGNORE INTO hosts (host) VALUES (?)", (host,))
        for k, v in cols.items():
            c.execute(f"UPDATE hosts SET {k} = ? WHERE host = ?", (v, host))  # noqa: S608


def set_meta(store: Store, key: str, value: str) -> None:
    with store.transaction() as c:
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def jlist(capsys: pytest.CaptureFixture[str], *argv: str) -> dict[str, Any]:
    code, out, _ = run(capsys, "list", "--json", *argv)
    assert code == 0, out
    d = json.loads(out)
    assert_valid("cli/list", d)
    return d  # type: ignore[no-any-return]


def check_golden(name: str, actual: Any, *, text: bool = False) -> None:
    path = GOLDEN / (f"{name}.txt" if text else f"{name}.json")
    if not path.exists():  # first run: write it, fail, and review the file before committing
        path.write_text(actual if text else json.dumps(actual, indent=2, sort_keys=True) + "\n")
        pytest.fail(f"golden {path.name} was missing and has been written; review it")
    expected = path.read_text()
    assert actual == expected if text else actual == json.loads(expected)


def populate(store: Store) -> None:
    """Two owners, labels, a per-PR watch, a query watch with members, terminal and unreachable
    PRs, and a paused host."""
    add_watch(store, "w-aaaaaaaaaa", "pr", "P1", {A: ["team=x"], B: ["team=y", "tier=1"]})
    add_pr(store, "P1", "github.com/o/r#1", dig=digest(), sources=())
    link(store, "P1", "w-aaaaaaaaaa")
    add_watch(store, "w-bbbbbbbbbb", "query", "mine+open;host=github.com", {A: ["team=x"]}, T1)
    add_pr(
        store, "P2", "github.com/o/r#2", status="terminal", dig=digest("merged"),
        terminal_at=T2, terminal_kind="merged",
    )  # fmt: skip
    link(store, "P2", "w-bbbbbbbbbb")
    add_pr(
        store, "P3", "github.com/o/s#3", status="unreachable", dig=digest(),
        failure_count=3, incomplete='{"ci": 2, "reviews": 1}',
    )  # fmt: skip
    link(store, "P3", "w-bbbbbbbbbb")
    add_pr(store, "P4", "github.com/o/s#4", dig=None)  # not yet baselined
    link(store, "P4", "w-bbbbbbbbbb")
    stuck = json.loads(digest(ci="pending"))
    stuck["draft"] = True
    add_pr(
        store, "P6", "github.com/o/s#6", dig=json.dumps(stuck), unsettled_since=T0
    )  # pending for far longer than ci.pending_max_age
    link(store, "P6", "w-bbbbbbbbbb")
    set_host(store, "github.com", paused_until=T2, last_enumerated_at=T2, last_full_at=T1)
    set_host(store, "gitlab.example", paused_until=None)
    set_meta(store, "last_poll_finished_at", T2)


# --- shapes and goldens ------------------------------------------------------------------------


def test_missing_state_dir_is_an_empty_store_and_creates_nothing(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    d = jlist(capsys)
    check_golden("empty", d)
    assert not state_dir.exists()


def test_existing_state_dir_without_a_store_creates_nothing(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir.mkdir(mode=0o700, parents=True)
    assert jlist(capsys) == {"watches": [], "prs": [], "last_poll_finished_at": None, "hosts": []}
    assert list(state_dir.iterdir()) == []


def test_empty_store(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    assert jlist(capsys) == {"watches": [], "prs": [], "last_poll_finished_at": None, "hosts": []}


def test_full_listing_json_golden(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    populate(store)
    d = jlist(capsys)
    check_golden("full", d)
    assert [w["id"] for w in d["watches"]] == ["w-aaaaaaaaaa", "w-bbbbbbbbbb"]
    assert [p["pr"] for p in d["prs"]] == [
        "github.com/o/r#1",
        "github.com/o/r#2",
        "github.com/o/s#3",
        "github.com/o/s#4",
        "github.com/o/s#6",
    ]
    assert [h["host"] for h in d["hosts"]] == ["github.com", "gitlab.example"]


def test_query_watch_members_and_pending_baseline(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(store)
    q = next(w for w in jlist(capsys)["watches"] if w["kind"] == "query")
    assert (q["members"], q["pending_baseline"]) == (4, 1)
    # the host was enumerated after the watch was created: derived, not null
    assert q["enumerated_at"] == T2


def test_unfiltered_query_enumerated_at_stays_null(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_watch(store, "w-cccccccccc", "query", "mine+open", {A: []})
    set_host(store, "github.com", last_enumerated_at=T2)
    assert jlist(capsys)["watches"][0]["enumerated_at"] is None


def test_pr_rows_are_the_shared_pr_row(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    populate(store)
    by = {p["pr_id"]: p for p in jlist(capsys)["prs"]}
    assert by["P2"]["terminal_kind"] == "merged" and by["P2"]["terminal_at"] == T2
    assert by["P3"]["status"] == "unreachable" and by["P3"]["failures"] == 3
    assert by["P3"]["incomplete"] == ["ci", "reviews"] and by["P3"]["incomplete_snapshots"] == 2
    assert by["P4"]["state"] is None and by["P4"]["last_polled_at"] is None
    assert by["P6"]["ci_stuck"] is True and by["P6"]["draft"] is True
    assert by["P1"]["ci_stuck"] is False
    assert by["P1"]["sources"] == ["w-aaaaaaaaaa"]
    assert by["P1"]["last_full_at"] == T1


# --- filters -----------------------------------------------------------------------------------


def test_owner_filter_selects_watches_and_their_prs(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(store)
    add_watch(store, "w-dddddddddd", "pr", "P7", {B: []})
    add_pr(store, "P7", "github.com/o/t#7", dig=digest())
    link(store, "P7", "w-dddddddddd")
    d = jlist(capsys, "--owner", B)
    assert [w["id"] for w in d["watches"]] == ["w-aaaaaaaaaa", "w-dddddddddd"]
    assert [p["pr_id"] for p in d["prs"]] == ["P1", "P7"]
    assert len(d["hosts"]) == 2  # hosts are never filtered


def test_a_selected_watch_keeps_all_its_owners(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(store)
    d = jlist(capsys, "--owner", B)
    assert d["watches"][0]["owners"] == [B, A]


def test_label_filter_is_and_over_the_labels(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(store)
    ids = lambda *a: [w["id"] for w in jlist(capsys, *a)["watches"]]  # noqa: E731
    assert ids("--label", "team=x") == ["w-aaaaaaaaaa", "w-bbbbbbbbbb"]
    assert ids("--label", "team=x", "--label", "tier=1") == ["w-aaaaaaaaaa"]
    assert ids("--label", "team=zzz") == []
    assert jlist(capsys, "--label", "team=zzz")["prs"] == []


def test_owner_and_label_together_use_that_owners_own_labels(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(store)
    # tier=1 belongs to B's reference only
    assert [w["id"] for w in jlist(capsys, "--owner", A, "--label", "tier=1")["watches"]] == []
    assert [w["id"] for w in jlist(capsys, "--owner", B, "--label", "tier=1")["watches"]] == [
        "w-aaaaaaaaaa"
    ]


def test_status_filters_prs_only(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    populate(store)
    d = jlist(capsys, "--status", "unreachable")
    assert [p["pr_id"] for p in d["prs"]] == ["P3"]
    assert len(d["watches"]) == 2 and len(d["hosts"]) == 2
    assert [p["pr_id"] for p in jlist(capsys, "--status", "terminal")["prs"]] == ["P2"]


def test_pr_filter_by_name_url_and_id(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    populate(store)
    for ref in ("github.com/o/r#1", "https://GITHUB.com/o/r/pull/1", "P1"):
        assert [p["pr_id"] for p in jlist(capsys, "--pr", ref)["prs"]] == ["P1"]
    assert len(jlist(capsys, "--pr", "github.com/o/r#1")["watches"]) == 2


def test_pr_filter_for_an_unstored_pr_is_empty_not_an_error(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(store)
    assert jlist(capsys, "--pr", "github.com/o/r#99")["prs"] == []


def test_pr_filter_combines_with_owner(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    populate(store)
    assert jlist(capsys, "--owner", A, "--pr", "P1")["prs"][0]["pr_id"] == "P1"
    add_watch(store, "w-dddddddddd", "pr", "P7", {"human:z": []})
    assert jlist(capsys, "--owner", "human:z", "--pr", "P1")["prs"] == []


def test_bad_filters_are_usage_errors(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "list", "--json", "--label", "nokv")
    assert code == 2 and json.loads(out)["error"]["code"] == "usage"
    assert run(capsys, "list", "--json", "--status", "weird")[0] == 2


# --- read-only ---------------------------------------------------------------------------------


def test_list_writes_nothing(
    store: Store, state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(store)
    before = {
        t: store.conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()  # noqa: S608
        for t in ("meta", "hosts", "watches", "prs", "sources", "events", "leases")
    }
    changes = store.conn.total_changes
    jlist(capsys)
    run(capsys, "list")
    after = {
        t: store.conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()  # noqa: S608
        for t in before
    }
    assert before == after and store.conn.total_changes == changes
    assert not (state_dir / "log.jsonl").exists()


def test_a_sqlite_error_while_reading_leaks_no_sqlite_text(
    store: Store, state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(store)
    store.close()
    db = sqlite3.connect(state_dir / "state.db")
    db.execute("DROP TABLE hosts")  # still a pr-watch database, but the read hits a sqlite error
    db.commit()
    db.close()
    code, out, err = run(capsys, "list", "--json")
    assert code == 1
    error = json.loads(out)["error"]
    assert error["code"] == "internal"
    assert error["message"] == "state.db cannot be used (SQLITE_ERROR)"
    assert "no such table" not in out + err and "hosts" not in out + err


# --- human output ------------------------------------------------------------------------------


def test_human_output_golden(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    populate(store)
    code, out, _ = run(capsys, "list")
    assert code == 0
    check_golden("full", out, text=True)


def test_human_output_of_an_empty_store(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "list")
    assert code == 0
    check_golden("empty", out, text=True)


def test_human_quiet_prints_nothing(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    populate(store)
    assert run(capsys, "list", "--quiet") == (0, "", "")


def test_hostile_stored_text_is_scrubbed_in_human_output(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    evil = "x\x1b[31m\nFORGED‮gnp\x00"
    add_watch(store, "w-eeeeeeeeee", "pr", "P9", {"human:a\nforged": [f"k={evil}"]})
    add_pr(store, "P9", f"github.com/o/r#9{evil}", dig=digest(), sources=())
    link(store, "P9", "w-eeeeeeeeee")
    set_host(store, f"h{evil}")
    code, out, _ = run(capsys, "list")
    assert code == 0
    assert "\x1b" not in out and "\x00" not in out and "‮" not in out
    assert "\nFORGED" not in out and "\nforged" not in out
    assert "<U+001B>" in out and "<U+000A>" in out
    for line in out.splitlines():
        assert line == line.strip("\r")


def test_hostile_long_text_is_bounded_in_human_output(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_watch(store, "w-ffffffffff", "pr", "P9", {A: ["k=" + "v" * 5000]})
    add_pr(store, "P9", "github.com/o/r#9", dig=digest())
    link(store, "P9", "w-ffffffffff")
    out = run(capsys, "list")[1]
    assert max(map(len, out.splitlines())) < 1000


def test_a_newer_store_is_exit_12(
    store: Store, state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store.close()
    db = sqlite3.connect(state_dir / "state.db")
    db.execute("PRAGMA journal_mode = DELETE")
    db.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    db.execute("PRAGMA user_version = 99")
    db.commit()
    db.close()
    code, out, _ = run(capsys, "list", "--json")
    assert code == 12 and json.loads(out)["error"]["code"] == "store_too_new"


# --- strictly read-only (R1, R2) ---------------------------------------------------------------


def run_child(code: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ},
        check=False,
    )


def fingerprint(state_dir: Path) -> dict[str, tuple[str, int] | None]:
    """sha256 and size of the db and the WAL (the data), and the names in the directory (-shm is
    a volatile index that any reader may touch: only its presence counts). Call it only while no
    connection of this process is open on the store."""
    out: dict[str, tuple[str, int] | None] = {}
    for name in sorted(os.listdir(state_dir)):
        path = state_dir / name
        if name in ("state.db", "state.db-wal"):
            data = path.read_bytes()
            out[name] = (hashlib.sha256(data).hexdigest(), len(data))
        else:
            out[name] = None
    return out


def settled(fp: dict[str, tuple[str, int] | None]) -> dict[str, tuple[str, int] | None]:
    """Without the litter SQLite itself gives a read-only reader of a WAL database when the
    directory lacks them: an empty -wal and a -shm (never written to the database file)."""
    drop = {
        k for k, v in fp.items() if k == "state.db-shm" or (k == "state.db-wal" and v and not v[1])
    }
    return {k: v for k, v in fp.items() if k not in drop}


LEAVE_WAL = """
    import os, sys
    from pathlib import Path
    from pr_watch.state_dir import open_state_dir
    from pr_watch.store import open_store
    st = open_state_dir(Path(sys.argv[1]))
    s = open_store(st)
    with s.transaction():
        s.conn.execute("INSERT INTO hosts (host) VALUES ('wal-only.example')")
    os._exit(0)  # no close: the commit stays in an uncheckpointed WAL
"""


def test_a_zero_byte_state_db_is_an_empty_store_and_is_not_touched(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir.mkdir(mode=0o700, parents=True)
    db = state_dir / "state.db"
    db.write_bytes(b"")
    db.chmod(0o600)
    before = fingerprint(state_dir)
    d = jlist(capsys)
    assert d == {"watches": [], "prs": [], "last_poll_finished_at": None, "hosts": []}
    run(capsys, "list")
    assert fingerprint(state_dir) == before == {"state.db": (hashlib.sha256().hexdigest(), 0)}


def test_list_leaves_the_state_directory_byte_identical(
    store: Store, state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(store)
    store.close()
    before = fingerprint(state_dir)
    assert jlist(capsys)["watches"]
    run(capsys, "list")
    assert settled(fingerprint(state_dir)) == settled(before)


def test_list_reads_a_leftover_wal_without_checkpointing_it(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    done = run_child(LEAVE_WAL, str(state_dir))
    assert done.returncode == 0, done.stderr
    before = fingerprint(state_dir)
    assert before["state.db-wal"] is not None and before["state.db-wal"][1] > 0
    d = jlist(capsys)
    assert [h["host"] for h in d["hosts"]] == ["wal-only.example"]  # the WAL is read
    after = fingerprint(state_dir)
    assert after == before  # nothing checkpointed, deleted or created


@pytest.mark.parametrize("dir_mode, file_mode", [(0o500, 0o600), (0o700, 0o400)])
def test_list_on_a_read_only_store_works_or_fails_cleanly_and_creates_nothing(
    store: Store,
    state_dir: Path,
    capsys: pytest.CaptureFixture[str],
    dir_mode: int,
    file_mode: int,
) -> None:
    populate(store)
    store.close()
    names = sorted(os.listdir(state_dir))
    (state_dir / "state.db").chmod(file_mode)
    state_dir.chmod(dir_mode)
    try:
        code, out, err = run(capsys, "list", "--json")
        if dir_mode == 0o500:
            # a clean WAL database with no -wal/-shm in a read-only directory is refused
            assert code == 1
            error = json.loads(out)["error"]
            assert error["code"] == "internal"
            assert "SQLITE_READONLY_DIRECTORY" in error["message"]
            assert set(os.listdir(state_dir)) == set(names)  # nothing created
        elif code == 0:
            assert len(json.loads(out)["watches"]) == 2
        else:
            assert json.loads(out)["error"]["code"] in {"internal", "state_unsafe"}
            assert "Traceback" not in out + err
        extra = set(os.listdir(state_dir)) - set(names)
        assert extra <= {"state.db-shm", "state.db-wal"}  # a WAL reader's empty files, no more
        assert set(names) <= set(os.listdir(state_dir))
    finally:
        state_dir.chmod(0o700)
        (state_dir / "state.db").chmod(0o600)


def test_list_does_not_wait_for_or_block_a_writer(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(store)
    # an open, uncommitted write transaction on another connection does not break list
    store.conn.execute("BEGIN IMMEDIATE")
    store.conn.execute("INSERT INTO hosts (host) VALUES ('uncommitted.example')")
    assert "uncommitted.example" not in [h["host"] for h in jlist(capsys)["hosts"]]
    store.conn.execute("COMMIT")
    assert "uncommitted.example" in [h["host"] for h in jlist(capsys)["hosts"]]

    # and a writer commits while list is reading: list still shows one consistent snapshot
    original = listing._pr_ids

    def commit_then_read(*a: Any, **k: Any) -> Any:
        set_host(store, "late.example")  # takes the write lock and commits mid-read
        return original(*a, **k)

    monkeypatch.setattr(listing, "_pr_ids", commit_then_read)
    seen = [h["host"] for h in jlist(capsys)["hosts"]]
    assert "late.example" not in seen
    monkeypatch.setattr(listing, "_pr_ids", original)
    assert "late.example" in [h["host"] for h in jlist(capsys)["hosts"]]


def test_open_existing_store_is_query_only(store: Store, state_dir: Path) -> None:
    store.close()
    with open_state_dir(state_dir) as state:
        ro = open_existing_store(state)
        assert ro is not None
        with ro:
            assert ro.conn.execute("PRAGMA query_only").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError):
                ro.conn.execute("INSERT INTO hosts (host) VALUES ('x')")


def test_open_existing_store_never_migrates_an_older_database(
    store: Store, state_dir: Path
) -> None:
    store.close()

    def v2(conn: sqlite3.Connection) -> None:
        conn.execute("ALTER TABLE hosts ADD COLUMN note TEXT")

    before = fingerprint(state_dir)
    with open_state_dir(state_dir) as state:
        with pytest.raises(StoreError) as info:
            open_existing_store(state, migrations=(*MIGRATIONS, v2))
    assert info.value.code == "internal" and "older" in str(info.value)
    assert settled(fingerprint(state_dir)) == settled(before)


# --- watches, PRs, errors (R3, R5, R6, R8, R4) ---------------------------------------------------


def test_pr_filter_selects_one_pr_and_does_not_narrow_watches_or_hosts(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(store)
    full = jlist(capsys)
    only = jlist(capsys, "--pr", "github.com/o/r#1")
    assert [p["pr_id"] for p in only["prs"]] == ["P1"]
    assert only["watches"] == full["watches"]
    assert only["hosts"] == full["hosts"]
    missing = jlist(capsys, "--pr", "github.com/o/r#99")
    assert missing["prs"] == [] and missing["watches"] == full["watches"]


def ready(view: dict[str, Any]) -> bool:
    """A query watch is ready when its enumeration is known and no member awaits a baseline."""
    return view["enumerated_at"] is not None and view["pending_baseline"] == 0


def view_of(store: Store, wid: str) -> dict[str, Any]:
    view = watch_view(store.conn, wid)
    assert view is not None
    return view


def test_query_watch_is_ready_with_baselined_members_and_an_enumerated_host(
    store: Store,
) -> None:
    add_watch(store, "w-rrrrrrrrr1", "query", "mine+open;host=h1.example", {A: []}, T0)
    add_pr(store, "P1", "h1.example/o/r#1", dig=digest(), sources=())
    link(store, "P1", "w-rrrrrrrrr1")
    set_host(store, "h1.example", last_enumerated_at=T2)
    view = view_of(store, "w-rrrrrrrrr1")
    assert view["enumerated_at"] == T2 and view["pending_baseline"] == 0
    assert ready(view) is True


def test_query_watch_is_not_ready_while_the_host_was_never_enumerated(store: Store) -> None:
    add_watch(store, "w-rrrrrrrrr2", "query", "mine+open;host=h1.example", {A: []}, T0)
    set_host(store, "h1.example", last_enumerated_at=None)
    view = view_of(store, "w-rrrrrrrrr2")
    assert view["enumerated_at"] is None and ready(view) is False
    assert ready(view_of(store, "w-rrrrrrrrr2")) is False  # no hosts row at all, same


def test_query_watch_is_not_ready_with_a_member_without_a_digest(store: Store) -> None:
    add_watch(store, "w-rrrrrrrrr3", "query", "mine+open;host=h1.example", {A: []}, T0)
    add_pr(store, "P1", "h1.example/o/r#1", dig=None, sources=())
    link(store, "P1", "w-rrrrrrrrr3")
    set_host(store, "h1.example", last_enumerated_at=T2)
    view = view_of(store, "w-rrrrrrrrr3")
    assert view["enumerated_at"] == T2 and view["pending_baseline"] == 1
    assert ready(view) is False


def test_query_watch_enumerated_at_is_the_oldest_host_and_null_if_any_is_null(
    store: Store,
) -> None:
    add_watch(store, "w-rrrrrrrrr4", "query", "mine+open;host=h1.example,h2.example", {A: []}, T0)
    set_host(store, "h1.example", last_enumerated_at=T2)
    assert view_of(store, "w-rrrrrrrrr4")["enumerated_at"] is None  # h2 has no row
    set_host(store, "h2.example", last_enumerated_at=None)
    assert view_of(store, "w-rrrrrrrrr4")["enumerated_at"] is None  # h2 is null
    set_host(store, "h2.example", last_enumerated_at=T1)
    view = view_of(store, "w-rrrrrrrrr4")
    assert view["enumerated_at"] == T1 and ready(view) is True  # the oldest of T1, T2


def test_query_watch_enumerated_before_it_was_created_is_null(store: Store) -> None:
    add_watch(store, "w-rrrrrrrrr5", "query", "mine+open;host=h1.example", {A: []}, T2)
    set_host(store, "h1.example", last_enumerated_at=T1)  # older than created_at
    view = view_of(store, "w-rrrrrrrrr5")
    assert view["enumerated_at"] is None and ready(view) is False


def test_a_pr_with_a_ci_hold_is_stale_with_no_ci_state(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    held = json.loads(digest())
    held["ci"]["hold"] = 2
    add_pr(store, "P5", "github.com/o/r#5", dig=json.dumps(held), sources=("w-sssssssssa",))
    row = jlist(capsys)["prs"][0]
    assert row["ci_stale"] is True and row["ci_state"] is None
    assert "ci=stale" in run(capsys, "list")[1]


def test_a_pending_pr_inside_the_age_limit_is_not_stuck(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    # NOW is 2026-06-02T00:00Z and ci.pending_max_age is 6h: 4h pending is not stuck, 7h is
    for pr_id, since in (
        ("P5", "2026-06-01T20:00:00.000000Z"),
        ("P6", "2026-06-01T17:00:00.000000Z"),
    ):
        add_pr(
            store, pr_id, f"github.com/o/r#{pr_id[1]}", dig=digest(ci="pending"),
            unsettled_since=since, sources=("w-sssssssssa",),
        )  # fmt: skip
    by = {p["pr_id"]: p["ci_stuck"] for p in jlist(capsys)["prs"]}
    assert by == {"P5": False, "P6": True}


def test_an_ambiguous_pr_name_is_exit_2(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    add_pr(store, "Q1", "github.com/o/r#5", dig=digest(), sources=("w-sssssssssa",))
    add_pr(store, "Q2", "github.com/o/r#5", dig=digest(), sources=("w-sssssssssa",))
    code, out, _ = run(capsys, "list", "--json", "--pr", "github.com/o/r#5")
    err = json.loads(out)["error"]
    assert code == 2 and err["code"] == "usage" and err["details"]["kind"] == "ambiguous_reference"
    assert [p["pr_id"] for p in jlist(capsys, "--pr", "Q1")["prs"]] == ["Q1"]  # the id works


def test_a_bad_owner_or_pr_value_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    for argv, code_name in (
        (("--owner", "Bad Owner"), "usage"),
        (("--pr", "not a pr#x"), "invalid_pr"),
        (("--pr", ""), "invalid_pr"),
    ):
        code, out, _ = run(capsys, "list", "--json", *argv)
        assert code == 2, argv
        assert json.loads(out)["error"]["code"] == code_name


def test_status_with_no_matching_pr_still_prints_the_watches(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_watch(store, "w-aaaaaaaaaa", "pr", "P1", {A: []})
    add_pr(store, "P1", "github.com/o/r#1", dig=digest())
    link(store, "P1", "w-aaaaaaaaaa")
    d = jlist(capsys, "--status", "terminal")
    assert d["prs"] == [] and [w["id"] for w in d["watches"]] == ["w-aaaaaaaaaa"]
    out = run(capsys, "list", "--status", "terminal")[1]
    assert "watches (1)" in out and "prs (0)" in out and "w-aaaaaaaaaa" in out
