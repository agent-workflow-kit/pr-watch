"""`list`: watches, PRs and host state, offline and read-only, through main()."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pr_watch.cli import main
from pr_watch.state_dir import open_state_dir
from pr_watch.store import Store, open_store
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


def test_an_unreadable_row_does_not_leak_sqlite_text(
    store: Store, state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    populate(store)
    store.close()
    db = sqlite3.connect(state_dir / "state.db")
    db.execute("PRAGMA journal_mode = DELETE")
    db.close()
    assert run(capsys, "list", "--json")[0] == 0


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
