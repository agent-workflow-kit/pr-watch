"""Poll steps 1 to 3 against the fake forgectl: host list, listings, membership (#21)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pr_watch import enumeration as en
from pr_watch.config import Config
from pr_watch.forgectl_client import ForgectlClient, resolve_executable
from pr_watch.state_dir import open_state_dir
from pr_watch.store import Store, format_ts, open_store
from tests.forgectl_support import FakeForgectl, new_pr

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
WATCH = "w-aaaaaaaaaa"
HOST = "github.com"


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


def run(store: Store, **kw: Any) -> en.EnumerationResult:
    return en.enumerate_scopes(store, make_client(), Config(), now=kw.pop("now", NOW), **kw)


def add_query(store: Store, key: str = "mine+open", watch: str = WATCH) -> str:
    store.conn.execute(
        "INSERT INTO watches VALUES (?, 'query', ?, '2026-01-01T00:00:00.000000Z')", (watch, key)
    )
    store.conn.execute("INSERT INTO owners VALUES (?, 'me')", (watch,))
    store.conn.commit()
    return watch


def add_pr_watch(store: Store, snap: dict[str, Any], **cols: Any) -> None:
    c = store.conn
    c.execute(
        "INSERT INTO prs (pr_id, pr, repo, host) VALUES (?, ?, ?, ?)",
        (snap["pr_id"], snap["pr"], snap["repo"], HOST),
    )
    for k, v in cols.items():
        c.execute(f"UPDATE prs SET {k} = ? WHERE pr_id = ?", (v, snap["pr_id"]))  # noqa: S608
    c.execute(
        "INSERT INTO watches VALUES (?, 'pr', ?, '2026-01-01T00:00:00.000000Z')",
        (
            "w-" + snap["pr_id"][-10:].lower().replace("_", "a").replace("0", "a")[:10],
            snap["pr_id"],
        ),
    )
    wid = c.execute("SELECT id FROM watches WHERE key = ?", (snap["pr_id"],)).fetchone()[0]
    c.execute("INSERT INTO sources (pr_id, watch_id) VALUES (?, ?)", (snap["pr_id"], wid))
    c.commit()


def pr(n: int, repo: str = "example-org/widgets", host: str = HOST, **ch: Any) -> dict[str, Any]:
    ch.setdefault("updated_at", "2026-10-02T10:30:00Z")
    return new_pr(host, repo, n, **ch)


def source(store: Store, snap: dict[str, Any], watch: str = WATCH) -> Any:
    return store.conn.execute(
        "SELECT left_count FROM sources WHERE pr_id = ? AND watch_id = ?", (snap["pr_id"], watch)
    ).fetchone()


def col(store: Store, snap: dict[str, Any], name: str) -> Any:
    row = store.conn.execute(
        f"SELECT {name} FROM prs WHERE pr_id = ?",
        (snap["pr_id"],),  # noqa: S608
    ).fetchone()
    return None if row is None else row[0]


def events(store: Store) -> int:
    return int(store.conn.execute("SELECT count(*) FROM events").fetchone()[0])


# --- first poll ---


def test_first_poll_with_many_members_adds_silent_rows(store: Store, fake: FakeForgectl) -> None:
    add_query(store)
    snaps = [pr(n) for n in range(1, 61)]
    for s in snaps:
        fake.add(s)
    res = run(store)
    assert len(res.new_members) == 60
    assert store.conn.execute("SELECT count(*) FROM sources").fetchone()[0] == 60
    row = store.conn.execute(
        "SELECT digest, rebaseline, status, listed_updated_at, host FROM prs WHERE pr_id = ?",
        (snaps[0]["pr_id"],),
    ).fetchone()
    assert tuple(row) == (None, 0, "active", "2026-10-02T10:30:00.000000Z", HOST)
    assert source(store, snaps[0])[0] == 0
    assert events(store) == 0
    assert res.candidates == {s["pr_id"] for s in snaps}
    args = fake.argvs("prs")
    assert len(args) == 1
    assert "--host=github.com" in args[0] and "--author=me" in args[0]
    assert "--state=open" in args[0] and "--limit=1000" in args[0]
    last = store.conn.execute("SELECT last_enumerated_at FROM hosts").fetchone()[0]
    assert last == format_ts(NOW)


def test_second_listing_is_idempotent(store: Store, fake: FakeForgectl) -> None:
    add_query(store)
    s = pr(1)
    fake.add(s)
    run(store)
    res = run(store)
    assert res.new_members == []
    assert store.conn.execute("SELECT count(*) FROM sources").fetchone()[0] == 1


def test_no_query_watch_means_no_host_call(store: Store, fake: FakeForgectl) -> None:
    fake.add(pr(1))
    run(store)
    assert fake.calls("prs") == [] and fake.calls("whoami") == []


# --- left_count ----------------------------------------------------------------------------


def test_member_leaves_and_returns(store: Store, fake: FakeForgectl) -> None:
    add_query(store)
    s = pr(1)
    fake.add(s)
    fake.add(pr(2))
    run(store)
    fake.remove(s["pr"])
    run(store)
    assert source(store, s)[0] == 1
    fake.add(s)
    run(store)
    assert source(store, s)[0] == 0
    fake.remove(s["pr"])
    run(store)
    run(store)
    run(store)
    assert source(store, s)[0] == 2  # capped; the snapshot commit removes it, not enumeration
    assert col(store, s, "pr_id") == s["pr_id"]


def test_absent_from_a_complete_listing_never_changes_status_or_makes_events(
    store: Store, fake: FakeForgectl
) -> None:
    add_query(store)
    s = pr(1)
    fake.add(s)
    run(store)
    fake.remove(s["pr"])
    run(store)
    run(store)
    assert col(store, s, "status") == "active"
    assert events(store) == 0


def test_a_merged_pr_leaves_the_open_listing_but_keeps_its_source(
    store: Store, fake: FakeForgectl
) -> None:
    add_query(store)
    s = pr(1)
    fake.add(s)
    run(store)
    fake.update(s["pr"], state="merged")
    run(store)
    run(store)
    assert source(store, s)[0] == 2
    assert col(store, s, "terminal_kind") is None  # only the snapshot sets terminal state


def test_incomplete_listing_never_counts_a_member_as_left(store: Store, fake: FakeForgectl) -> None:
    add_query(store)
    s = pr(1)
    fake.add(s)
    run(store)
    fake.remove(s["pr"])
    fake.add(pr(2))
    store.conn.execute("UPDATE sources SET left_count = 1")
    store.conn.commit()
    res = _force_incomplete(store, fake)
    assert res.hosts[HOST].incomplete_listing
    assert source(store, s)[0] == 1


def _force_incomplete(store: Store, fake: FakeForgectl) -> en.EnumerationResult:
    class Cut(ForgectlClient):
        def prs(self, **kw: Any) -> Any:
            out = super().prs(**kw)
            assert out.result is not None
            out.result["complete"] = False
            return out

    base = make_client()
    client = Cut(
        base.exe, timeout_ms=5000, max_output=4 * 1024 * 1024, env=dict(os.environ),
        deadline_slack=5.0, term_grace=0.3,
    )  # fmt: skip
    return en.enumerate_scopes(store, client, Config(), now=NOW)


def test_incomplete_listing_still_adds_members(store: Store, fake: FakeForgectl) -> None:
    add_query(store)
    fake.add(pr(1))
    res = _force_incomplete(store, fake)
    assert len(res.new_members) == 1


def test_member_that_stops_matching_repo_filter_counts_as_absent(
    store: Store, fake: FakeForgectl
) -> None:
    add_query(store, "mine+open;repo=github.com/example-org/widgets")
    a, b = pr(1), pr(2, repo="example-org/other")
    fake.add(a)
    fake.add(b)
    run(store)
    assert source(store, a) is not None and source(store, b) is None
    # the same PR (same pr_id) is now listed open under another repo: still listed, no match
    fake.remove(a["pr"])
    fake.add(pr(1, repo="example-org/other", pr_id=a["pr_id"]))
    run(store)
    assert source(store, a)[0] == 1


def test_host_filter_and_other_hosts_untouched(store: Store, fake: FakeForgectl) -> None:
    fake.host("gitlab.com", me="user-1")
    add_query(store, "mine+open;host=github.com")
    fake.add(pr(1))
    fake.add(pr(1, host="gitlab.com"))
    run(store)
    assert [a for a in fake.argvs("prs")] and all(
        "--host=github.com" in a for a in fake.argvs("prs")
    )
    assert store.conn.execute("SELECT count(*) FROM sources").fetchone()[0] == 1


def test_absence_on_one_host_does_not_touch_members_of_another(
    store: Store, fake: FakeForgectl
) -> None:
    fake.host("gitlab.com", me="user-1")
    add_query(store)
    a, b = pr(1), pr(1, host="gitlab.com")
    fake.add(a)
    fake.add(b)
    run(store)
    fake.remove(a["pr"])
    run(store)
    assert source(store, a)[0] == 1 and source(store, b)[0] == 0


# --- reactivation, listed_updated_at -------------------------------------------------------------


def test_listed_updated_at_only_grows_and_pr_is_refreshed(store: Store, fake: FakeForgectl) -> None:
    add_query(store)
    s = pr(1)
    fake.add(s)
    run(store)
    store.conn.execute(
        "UPDATE prs SET listed_updated_at = '2026-12-01T00:00:00.000000Z', "
        "pr = 'github.com/x/old#1'"
    )
    store.conn.commit()
    run(store)
    assert col(store, s, "listed_updated_at") == "2026-12-01T00:00:00.000000Z"
    assert col(store, s, "pr") == s["pr"]


def test_closed_terminal_row_listed_open_is_reactivated(store: Store, fake: FakeForgectl) -> None:
    add_query(store)
    s = pr(1)
    fake.add(s)
    store.conn.execute(
        "INSERT INTO prs (pr_id, pr, repo, host, status, terminal_kind, terminal_at) "
        "VALUES (?, ?, ?, ?, 'terminal', 'closed', '2026-10-01T00:00:00.000000Z')",
        (s["pr_id"], s["pr"], s["repo"], HOST),
    )
    store.conn.execute("INSERT INTO sources (pr_id, watch_id) VALUES (?, ?)", (s["pr_id"], WATCH))
    store.conn.commit()
    res = run(store)
    assert res.reactivated == [s["pr_id"]]
    assert tuple(
        store.conn.execute(
            "SELECT status, terminal_at, terminal_kind, rebaseline FROM prs"
        ).fetchone()
    ) == ("active", None, None, 1)


def test_merged_row_is_never_reactivated(store: Store, fake: FakeForgectl) -> None:
    add_query(store)
    s = pr(1)
    fake.add(s)
    store.conn.execute(
        "INSERT INTO prs (pr_id, pr, repo, host, status, terminal_kind, terminal_at) "
        "VALUES (?, ?, ?, ?, 'terminal', 'merged', '2026-10-01T00:00:00.000000Z')",
        (s["pr_id"], s["pr"], s["repo"], HOST),
    )
    store.conn.execute("INSERT INTO sources (pr_id, watch_id) VALUES (?, ?)", (s["pr_id"], WATCH))
    store.conn.commit()
    res = run(store)
    assert res.reactivated == [] and col(store, s, "status") == "terminal"


def test_same_pr_name_with_another_pr_id_is_another_pr(store: Store, fake: FakeForgectl) -> None:
    add_query(store)
    old = pr(1)
    add_pr_watch(store, old)
    new = pr(1, pr_id="PR_recreated")
    fake.add(new)
    run(store)
    assert col(store, new, "pr_id") == "PR_recreated"
    assert col(store, old, "listed_updated_at") is None


# --- host failures ---


@pytest.mark.parametrize("code", ["auth_required", "network_error", "permission_denied"])
def test_host_failure_changes_no_pr_and_is_listed(
    store: Store, fake: FakeForgectl, code: str
) -> None:
    add_query(store)
    s = pr(1)
    fake.add(s)
    run(store)
    fake.fail("prs", code, host=HOST)
    before = store.conn.execute("SELECT * FROM prs").fetchall()
    res = run(store)
    assert [f.kind for f in res.failures] == ["query" if code != "auth_required" else "host"]
    assert res.failures[0].ref == HOST and res.failures[0].code == code
    assert store.conn.execute("SELECT * FROM prs").fetchall() == before
    assert source(store, s)[0] == 0
    assert events(store) == 0


def test_auth_required_cancels_the_rest_of_the_host(store: Store, fake: FakeForgectl) -> None:
    add_query(store)
    a = pr(1)
    add_pr_watch(store, a, digest="{}", snap_updated_at="2026-10-02T10:00:00.000000Z")
    fake.fail("prs", "auth_required", host=HOST)
    res = run(store)
    assert res.hosts[HOST].cancelled
    assert len(fake.calls("prs")) == 1  # the repo listing is not attempted
    assert col(store, a, "failure_count") == 0


def test_rate_limited_host_reports_pause_and_cancels(store: Store, fake: FakeForgectl) -> None:
    add_query(store)
    fake.rate_limited("prs", retry_after=30, host=HOST)
    res = run(store)
    f = res.hosts[HOST]
    assert f.rate_limited and f.retry_after == 30 and f.cancelled
    assert res.failures == []
    assert (
        store.conn.execute(
            "SELECT count(*) FROM hosts WHERE last_enumerated_at IS NOT NULL"
        ).fetchone()[0]
        == 0
    )


def test_failed_host_does_not_stop_other_hosts(store: Store, fake: FakeForgectl) -> None:
    fake.host("gitlab.com", me="user-1")
    add_query(store)
    fake.add(pr(1, host="gitlab.com"))
    fake.network_error("prs", host=HOST)
    res = run(store)
    assert [f.ref for f in res.failures] == [HOST]
    assert store.conn.execute("SELECT count(*) FROM sources").fetchone()[0] == 1


def test_partial_listing_is_used_but_never_concludes_a_leave(
    store: Store, fake: FakeForgectl
) -> None:
    fake.host("gitlab.com", me="user-1")
    add_query(store)
    a, b = pr(1), pr(1, host="gitlab.com")
    fake.add(a)
    fake.add(b)
    run(store)
    fake.remove(b["pr"])
    fake.add(pr(2))
    # without --host, a bare listing is partial when one host fails; pr-watch asks per host, so
    # script the failure as a partial result on the host call
    fake.script("prs", {"kind": "raw", "stdout": json.dumps({
        "items": [], "complete": False,
        "errors": [{"host": "gitlab.com", "code": "network_error", "message": "x"}],
        "hosts_queried": ["gitlab.com"],
        "rate_limit": {"host": None, "remaining": None, "reset_at": None},
    }), "exit": 12}, host="gitlab.com")  # fmt: skip
    res = run(store)
    assert source(store, b)[0] == 0
    assert res.hosts["gitlab.com"].incomplete_listing
    assert [f.code for f in res.failures] == ["network_error"]


def test_garbage_listing_is_a_failure_not_a_leave(store: Store, fake: FakeForgectl) -> None:
    add_query(store)
    s = pr(1)
    fake.add(s)
    run(store)
    fake.garbage("prs", host=HOST)
    res = run(store)
    assert res.failures[0].code == "contract" and source(store, s)[0] == 0


@pytest.mark.parametrize(
    "listing",
    [
        {"updated_at": "not a time"},
        {"repo": "github.com/other/repo"},
        {"pr": "gitlab.com/example-org/widgets#2"},
    ],  # the fake validates its output: shapes the pinned schema forbids are unit-tested below
)
def test_hostile_item_is_never_stored_and_never_makes_a_member_leave(
    store: Store, fake: FakeForgectl, listing: dict[str, Any]
) -> None:
    add_query(store)
    victim = pr(3)
    fake.add(victim)
    run(store)
    fake.add(pr(2), listing=listing)
    fake.remove(victim["pr"])
    fake.add(victim)
    fake.remove(victim["pr"])
    res = run(store)
    host = res.hosts[HOST]
    assert host.incomplete_listing or [f.code for f in res.failures] == ["contract"]
    assert source(store, victim)[0] == 0
    assert store.conn.execute("SELECT count(*) FROM prs").fetchone()[0] == 1


def test_hostile_item_is_rejected_by_the_stage_itself() -> None:
    good = {
        "pr": "github.com/o/r#1", "repo": "github.com/o/r", "pr_id": "PR_1", "state": "open",
        "updated_at": "2026-10-02T10:30:00+02:00",
    }  # fmt: skip
    accepted = en._item(good, HOST)
    assert accepted is not None and accepted.updated_at == "2026-10-02T08:30:00.000000Z"
    for change in (
        {"pr_id": "x" * 300}, {"pr_id": "a\u202eb"}, {"updated_at": "nope"},
        {"updated_at": "2026-10-02T10:00:00"}, {"repo": "github.com/o/other"}, {"pr": None},
        {"pr": "gitlab.com/o/r#1", "repo": "gitlab.com/o/r"}, {"state": 5}, {"pr": "../x#1"},
    ):  # fmt: skip
        assert en._item({**good, **change}, HOST) is None, change
    assert en._item("not a dict", HOST) is None


# --- whoami ---


def test_whoami_is_cached_for_the_ttl(store: Store, fake: FakeForgectl) -> None:
    add_query(store)
    fake.add(pr(1))
    run(store)
    run(store, now=NOW + timedelta(minutes=59))
    assert len(fake.calls("whoami")) == 1
    run(store, now=NOW + timedelta(hours=1, seconds=1))
    assert len(fake.calls("whoami")) == 2


def test_cache_dated_in_the_future_is_refetched(store: Store, fake: FakeForgectl) -> None:
    add_query(store)
    run(store)
    run(store, now=NOW - timedelta(hours=2))
    assert len(fake.calls("whoami")) == 2


def test_stale_cache_is_reused_with_a_warning_when_whoami_fails(
    store: Store, fake: FakeForgectl
) -> None:
    add_query(store)
    fake.add(pr(1))
    run(store)
    fake.garbage("whoami")
    res = run(store, now=NOW + timedelta(hours=3))
    assert res.host_list.stale and res.warnings
    assert res.stale_whoami is not None and res.failures == []
    assert len(res.new_members) == 0 and fake.calls("prs")


def test_no_cache_and_whoami_failure_skips_queries_and_lists_failure(
    store: Store, fake: FakeForgectl
) -> None:
    add_query(store)
    fake.add(pr(1))
    fake.garbage("whoami")
    res = run(store)
    assert not res.host_list.available
    assert [(f.kind, f.ref, f.code) for f in res.failures] == [("host", None, "contract")]
    assert fake.calls("prs") == []


def test_whoami_rate_limit_is_reported(store: Store, fake: FakeForgectl) -> None:
    add_query(store)
    envelope = {"error": {"code": "rate_limited", "message": "slow down", "retry_after": 12}}
    fake.raw("whoami", stdout=json.dumps(envelope), exit=12)
    res = run(store)
    assert res.whoami_retry_after == 12 and fake.calls("prs") == []


def test_whoami_rate_limit_of_one_host_pauses_that_host(store: Store, fake: FakeForgectl) -> None:
    add_query(store)
    fake.rate_limited("whoami", host=HOST)
    res = run(store)
    assert res.hosts[HOST].rate_limited and res.hosts[HOST].cancelled
    assert HOST in res.host_list.failed and res.failures == []
    assert fake.calls("prs") == []


def test_host_without_credential_is_skipped(store: Store, fake: FakeForgectl) -> None:
    fake.host(
        "gitlab.com", me="u", whoami={"login": None, "token_source": "none", "id": None,
                                      "me": None, "me_matches_login": None, "scopes": None},
    )  # fmt: skip
    add_query(store, "mine+open;host=gitlab.com")
    res = run(store)
    assert res.hosts["gitlab.com"].skipped == "no_credential"
    assert fake.calls("prs") == []


def test_unknown_host_in_filter_is_not_in_scope(store: Store, fake: FakeForgectl) -> None:
    add_query(store, "mine+open;host=nowhere.example.test")
    res = run(store)
    assert res.hosts["nowhere.example.test"].skipped == "not_in_scope"


def test_partial_whoami_marks_failed_hosts_and_is_not_cached(
    store: Store, fake: FakeForgectl
) -> None:
    fake.host("gitlab.com", me="user-1")
    add_query(store)
    fake.add(pr(1))
    fake.add(pr(1, host="gitlab.com"))
    fake.fail("whoami", "network_error", host="gitlab.com")
    res = run(store)
    assert "gitlab.com" in res.host_list.failed
    assert any(f.ref == "gitlab.com" for f in res.failures)
    assert [a for a in fake.argvs("prs") if "--host=gitlab.com" in a] == []
    assert (
        store.conn.execute("SELECT count(*) FROM meta WHERE key = 'hosts_cache'").fetchone()[0] == 0
    )


def test_a_corrupt_cache_is_ignored(store: Store, fake: FakeForgectl) -> None:
    add_query(store)
    store.conn.execute("INSERT INTO meta VALUES ('hosts_cache', '{not json')")
    store.conn.commit()
    run(store)
    assert len(fake.calls("whoami")) == 1


# --- transactions ---


def test_watch_removed_while_listing_gets_no_source(store: Store, fake: FakeForgectl) -> None:
    add_query(store)
    fake.add(pr(1))

    class Racing(ForgectlClient):
        def prs(self, **kw: Any) -> Any:
            out = super().prs(**kw)
            store.conn.execute("DELETE FROM watches WHERE id = ?", (WATCH,))
            store.conn.commit()
            return out

    base = make_client()
    client = Racing(base.exe, timeout_ms=5000, max_output=4 * 1024 * 1024, env=dict(os.environ))
    res = en.enumerate_scopes(store, client, Config(), now=NOW)
    assert res.new_members == []
    assert store.conn.execute("SELECT count(*) FROM sources").fetchone()[0] == 0
    assert store.conn.execute("SELECT count(*) FROM prs").fetchone()[0] == 0


def test_membership_applied_to_a_new_watch_only_after_it_exists(
    store: Store, fake: FakeForgectl
) -> None:
    add_query(store)
    s = pr(1)
    fake.add(s)

    class Racing(ForgectlClient):
        def prs(self, **kw: Any) -> Any:
            out = super().prs(**kw)
            add_query(store, "mine+open;repo=github.com/example-org/widgets", "w-bbbbbbbbbb")
            return out

    base = make_client()
    client = Racing(base.exe, timeout_ms=5000, max_output=4 * 1024 * 1024, env=dict(os.environ))
    en.enumerate_scopes(store, client, Config(), now=NOW)
    assert source(store, s, WATCH) is not None and source(store, s, "w-bbbbbbbbbb") is None
    en.enumerate_scopes(store, make_client(), Config(), now=NOW)
    assert source(store, s, "w-bbbbbbbbbb")[0] == 0


def test_a_failing_transaction_leaves_the_store_unchanged(
    store: Store, fake: FakeForgectl, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_query(store)
    for n in range(1, 4):
        fake.add(pr(n))
    real = en._join
    calls = {"n": 0}

    def boom(*a: Any, **k: Any) -> bool:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("boom")
        return real(*a, **k)

    monkeypatch.setattr(en, "_join", boom)
    with pytest.raises(RuntimeError):
        run(store)
    assert store.conn.execute("SELECT count(*) FROM prs").fetchone()[0] == 0
    assert store.conn.execute("SELECT count(*) FROM hosts").fetchone()[0] == 0


# --- per-PR repo scopes ---


REPO = "github.com/example-org/widgets"


def scope(store: Store) -> Any:
    return store.conn.execute("SELECT repo, watermark FROM scopes").fetchall()


def per_pr(
    store: Store, n: int = 1, snap_at: str | None = "2026-10-02T10:00:00.000000Z", **ch: Any
):
    s = pr(n, **ch)
    add_pr_watch(store, s, digest="{}" if snap_at else None, snap_updated_at=snap_at)
    return s


def test_first_listing_starts_at_the_smallest_snap_time_minus_overlap(
    store: Store, fake: FakeForgectl
) -> None:
    per_pr(store, 1, "2026-10-02T10:00:00.000000Z")
    per_pr(store, 2, "2026-10-02T09:00:00.000000Z")
    fake.add(pr(1))
    run(store)
    argv = fake.argvs("prs")[0]
    assert f"--repo={REPO}" in argv and "--state=all" in argv
    assert "--updated-since=2026-10-02T08:59:00Z" in argv
    assert scope(store) == [(REPO, "2026-10-02T10:30:00.000000Z")]


def test_later_listing_uses_watermark_minus_overlap_and_advances(
    store: Store, fake: FakeForgectl
) -> None:
    s = per_pr(store)
    fake.add(pr(1))
    run(store)
    fake.update(s["pr"], updated_at="2026-10-02T11:45:00Z")
    res = run(store)
    assert "--updated-since=2026-10-02T10:29:00Z" in fake.argvs("prs")[1]
    assert scope(store)[0][1] == "2026-10-02T11:45:00.000000Z"
    assert col(store, s, "listed_updated_at") == "2026-10-02T11:45:00.000000Z"
    assert s["pr_id"] in res.candidates and res.scopes_listed == [REPO, REPO][:1]


def test_empty_listing_keeps_the_old_watermark_and_creates_one_when_absent(
    store: Store, fake: FakeForgectl
) -> None:
    per_pr(store, 1, "2026-10-02T10:00:00.000000Z")
    run(store)
    assert scope(store) == [(REPO, "2026-10-02T10:00:00.000000Z")]
    store.conn.execute("UPDATE scopes SET watermark = '2026-10-02T11:00:00.000000Z'")
    store.conn.commit()
    run(store)
    assert scope(store)[0][1] == "2026-10-02T11:00:00.000000Z"


def test_repo_without_digest_rows_is_not_listed(store: Store, fake: FakeForgectl) -> None:
    per_pr(store, 1, snap_at=None)
    run(store)
    assert fake.calls("prs") == [] and scope(store) == []


def test_repo_listing_failure_writes_nothing_and_names_the_repo(
    store: Store, fake: FakeForgectl
) -> None:
    s = per_pr(store)
    fake.not_found("prs", repo=REPO)
    res = run(store)
    assert [(f.kind, f.ref, f.code) for f in res.failures] == [("repo", REPO, "not_found")]
    assert scope(store) == []
    assert col(store, s, "failure_count") == 0 and col(store, s, "status") == "active"
    assert events(store) == 0


def test_incomplete_repo_listing_marks_active_per_pr_rows_changed(
    store: Store, fake: FakeForgectl
) -> None:
    a = per_pr(store, 1, "2026-10-02T10:00:00.000000Z")
    b = per_pr(store, 2, "2026-10-02T10:00:00.000000Z")
    store.conn.execute("UPDATE prs SET status = 'unreachable' WHERE pr_id = ?", (b["pr_id"],))
    store.conn.commit()
    fake.add(pr(1))
    fake.script("prs", {"kind": "raw", "stdout": json.dumps({
        "items": [], "complete": False, "errors": [], "hosts_queried": [HOST],
        "rate_limit": {"host": HOST, "remaining": 5, "reset_at": None},
    }), "exit": 0}, repo=REPO)  # fmt: skip
    run(store)
    assert col(store, a, "listed_updated_at") == "2026-10-02T10:00:00.000001Z"
    assert col(store, b, "listed_updated_at") is None


def test_complete_repo_listing_marks_nothing(store: Store, fake: FakeForgectl) -> None:
    a = per_pr(store, 1, "2026-10-02T10:30:00.000000Z")
    fake.add(pr(1))
    run(store)
    assert col(store, a, "listed_updated_at") == "2026-10-02T10:30:00.000000Z"


def test_closed_per_pr_row_inside_expiry_is_listed_and_reactivated(
    store: Store, fake: FakeForgectl
) -> None:
    s = pr(1)
    add_pr_watch(
        store, s, digest="{}", snap_updated_at="2026-10-02T10:00:00.000000Z",
        status="terminal", terminal_kind="closed", terminal_at="2026-10-02T09:00:00.000000Z",
    )  # fmt: skip
    fake.add(s)
    res = run(store)
    assert res.reactivated == [s["pr_id"]]
    assert col(store, s, "status") == "active" and col(store, s, "rebaseline") == 1


def test_closed_row_past_expiry_and_merged_rows_are_not_listed(
    store: Store, fake: FakeForgectl
) -> None:
    old = pr(1)
    add_pr_watch(store, old, digest="{}", snap_updated_at="2026-10-02T10:00:00.000000Z",
                 status="terminal", terminal_kind="closed",
                 terminal_at="2026-01-01T00:00:00.000000Z")  # fmt: skip
    merged = pr(2, repo="example-org/m")
    add_pr_watch(store, merged, digest="{}", snap_updated_at="2026-10-02T10:00:00.000000Z",
                 status="terminal", terminal_kind="merged",
                 terminal_at="2026-10-02T09:00:00.000000Z")  # fmt: skip
    run(store)
    assert fake.calls("prs") == []


def test_orphan_scope_rows_are_deleted_and_a_rename_starts_a_new_scope(
    store: Store, fake: FakeForgectl
) -> None:
    s = per_pr(store)
    fake.add(pr(1))
    run(store)
    renamed = "github.com/example-org/gadgets"
    store.conn.execute(
        "UPDATE prs SET repo = ?, pr = 'github.com/example-org/gadgets#7' WHERE pr_id = ?",
        (renamed, s["pr_id"]),
    )
    store.conn.commit()
    fake.add(
        pr(7, repo="example-org/gadgets", pr_id=s["pr_id"])
        if False
        else pr(7, "example-org/gadgets")
    )
    run(store)
    repos = [r[0] for r in scope(store)]
    assert repos == [renamed]


def test_repo_restriction_lists_that_repo_and_its_host_query(
    store: Store, fake: FakeForgectl
) -> None:
    add_query(store)
    per_pr(store, 1)
    per_pr(store, 2, repo="example-org/other")
    run(store, restrict_repo=REPO)
    assert len(fake.calls("whoami")) == 1
    argvs = fake.argvs("prs")
    assert len(argvs) == 2
    assert any("--host=github.com" in a for a in argvs)
    assert [a for a in argvs if f"--repo={REPO}" in a]


def test_restricted_poll_joins_only_the_repo_but_records_every_listed_row(
    store: Store, fake: FakeForgectl
) -> None:
    add_query(store)
    inside, outside, other = pr(1), pr(2, repo="example-org/other"), pr(3, repo="example-org/o3")
    for s in (inside, outside, other):
        fake.add(s)
    add_pr_watch(store, other)  # a stored row of another repo, listed by the host listing
    gone = pr(4)  # a member the listing no longer shows
    add_pr_watch(store, gone)
    store.conn.execute("INSERT INTO sources VALUES (?, ?, 1)", (gone["pr_id"], WATCH))
    store.conn.commit()
    res = run(store, restrict_repo=REPO.upper().replace("GITHUB.COM", "github.com"))
    assert res.new_members == [inside["pr_id"]]
    assert source(store, outside) is None and col(store, outside, "pr_id") is None
    assert col(store, other, "listed_updated_at") == "2026-10-02T10:30:00.000000Z"
    assert source(store, gone)[0] == 1  # no absence counting in a restricted poll
    assert store.conn.execute("SELECT count(*) FROM hosts").fetchone()[0] == 0


def test_restricted_poll_resets_left_count_of_the_repo_only(
    store: Store, fake: FakeForgectl
) -> None:
    add_query(store)
    a, b = pr(1), pr(2, repo="example-org/other")
    fake.add(a)
    fake.add(b)
    run(store)
    store.conn.execute("UPDATE sources SET left_count = 1")
    store.conn.commit()
    run(store, restrict_repo=REPO)
    assert source(store, a)[0] == 0 and source(store, b)[0] == 1


def test_restricted_poll_ignores_query_watches_of_other_hosts(
    store: Store, fake: FakeForgectl
) -> None:
    fake.host("gitlab.com", me="user-1")
    add_query(store, "mine+open;host=gitlab.com")
    run(store, restrict_repo=REPO)
    assert fake.calls("prs") == []


def test_restricted_poll_matches_the_stored_repo_case_insensitively(
    store: Store, fake: FakeForgectl
) -> None:
    s = pr(1, repo="example-org/Widgets")
    add_pr_watch(store, s, digest="{}", snap_updated_at="2026-10-02T10:00:00.000000Z")
    fake.add(s)
    run(store, restrict_repo=REPO)
    assert len(fake.calls("prs")) == 1 and scope(store)


def test_paused_host_is_not_called(store: Store, fake: FakeForgectl) -> None:
    add_query(store)
    per_pr(store, 1)
    res = run(store, paused_hosts={HOST})
    assert fake.calls("prs") == [] and res.hosts[HOST].cancelled


def test_should_stop_prevents_calls(store: Store, fake: FakeForgectl) -> None:
    add_query(store)
    res = run(store, should_stop=lambda: True)
    assert res.interrupted and fake.calls() == []


def test_rate_reading_is_kept_per_host(store: Store, fake: FakeForgectl) -> None:
    add_query(store)
    res = run(store)
    assert res.hosts[HOST].lowest_remaining == 4000


# --- review round 1 ---


def test_a_host_failure_of_a_repo_listing_is_named_by_the_host(
    store: Store, fake: FakeForgectl
) -> None:
    per_pr(store)
    fake.fail("prs", "auth_required", repo=REPO)
    res = run(store)
    assert [(f.kind, f.ref, f.code) for f in res.failures] == [("host", HOST, "auth_required")]


def test_a_transient_query_failure_does_not_cancel_the_repo_listings(
    store: Store, fake: FakeForgectl
) -> None:
    add_query(store)
    per_pr(store)
    fake.fail("prs", "network_error", argv_has=["--author=me"])
    res = run(store)
    assert not res.hosts[HOST].cancelled
    assert len(fake.calls("prs")) == 2 and res.scopes_listed == [REPO]


def test_a_failed_whoami_host_is_failed_not_skipped(store: Store, fake: FakeForgectl) -> None:
    fake.host("gitlab.com", me="user-1")
    add_query(store, "mine+open;host=gitlab.com")
    fake.fail("whoami", "network_error", host="gitlab.com")
    res = run(store)
    assert "gitlab.com" in res.host_list.failed
    assert res.facts("gitlab.com").skipped is None


def test_a_renamed_repo_is_refreshed_by_its_old_scope_listing(
    store: Store, fake: FakeForgectl
) -> None:
    s = per_pr(store)
    new = "github.com/example-org/gadgets"
    fake.add(s, listing={"repo": new, "pr": f"{new}#1", "updated_at": "2026-10-02T11:00:00Z"})
    res = run(store)
    assert col(store, s, "repo") == new and col(store, s, "pr") == f"{new}#1"
    assert col(store, s, "listed_updated_at") == "2026-10-02T11:00:00.000000Z"
    assert scope(store) == [(REPO, "2026-10-02T11:00:00.000000Z")]
    assert s["pr_id"] in res.candidates and res.new_members == []


@pytest.mark.parametrize("pr_id", ["--evil", ".x", "x" * 129, "a..b", "a b", ""])
def test_pr_id_outside_the_store_pattern_is_malformed(pr_id: str) -> None:
    good = {
        "pr": "github.com/o/r#1", "repo": "github.com/o/r", "pr_id": pr_id, "state": "open",
        "updated_at": "2026-10-02T10:30:00Z",
    }  # fmt: skip
    assert en._item(good, HOST) is None


def test_a_stored_row_of_another_host_is_not_touched_by_the_same_pr_id(
    store: Store, fake: FakeForgectl
) -> None:
    fake.host("gitlab.com", me="user-1")
    add_query(store, "mine+open;host=gitlab.com")
    mine = pr(1)
    add_pr_watch(store, mine)
    fake.add(pr(1, host="gitlab.com", pr_id=mine["pr_id"]))
    res = run(store)
    assert col(store, mine, "pr") == mine["pr"] and col(store, mine, "listed_updated_at") is None
    assert source(store, mine, WATCH) is None and res.new_members == []
    assert res.candidates == set()


def test_a_future_updated_at_does_not_become_the_watermark(
    store: Store, fake: FakeForgectl
) -> None:
    s = per_pr(store)
    fake.add(pr(1, updated_at="2099-01-01T00:00:00Z"))
    run(store)
    assert scope(store) == [(REPO, format_ts(NOW))]
    assert col(store, s, "listed_updated_at") == "2099-01-01T00:00:00.000000Z"


@pytest.mark.parametrize("stamp", ["2026-10-02t10:30:00z", "2026-10-02T10:30:00z"])
def test_lowercase_t_and_z_are_accepted(stamp: str) -> None:
    got = en.parse_stamp(stamp)
    assert got == datetime(2026, 10, 2, 10, 30, tzinfo=UTC)


def test_updated_at_before_1970_is_malformed() -> None:
    good = {
        "pr": "github.com/o/r#1", "repo": "github.com/o/r", "pr_id": "PR_1", "state": "open",
        "updated_at": "0001-01-01T00:00:00Z",
    }  # fmt: skip
    assert en._item(good, HOST) is None
    assert en._item({**good, "updated_at": "1970-01-01T00:00:00Z"}, HOST) is not None


def test_extreme_snap_time_in_an_incomplete_listing_does_not_raise(
    store: Store, fake: FakeForgectl
) -> None:
    a = per_pr(store, 1, "9999-12-31T23:59:59.999999Z")
    fake.script("prs", {"kind": "raw", "stdout": json.dumps({
        "items": [], "complete": False, "errors": [], "hosts_queried": [HOST],
        "rate_limit": {"host": HOST, "remaining": 5, "reset_at": None},
    }), "exit": 0}, repo=REPO)  # fmt: skip
    run(store)
    assert col(store, a, "listed_updated_at") is None


def test_extreme_watermark_is_treated_as_no_watermark(store: Store, fake: FakeForgectl) -> None:
    per_pr(store)
    store.conn.execute(
        "INSERT INTO scopes (repo, watermark) VALUES (?, '0001-01-01T00:00:00.000000Z')", (REPO,)
    )
    store.conn.commit()
    run(store)
    assert "--updated-since=2026-10-02T09:59:00Z" in fake.argvs("prs")[0]


def test_restricted_poll_reports_only_the_restricted_hosts_whoami_problems(
    store: Store, fake: FakeForgectl
) -> None:
    fake.host("gitlab.com", me="user-1")
    add_query(store)
    fake.add(pr(1))
    fake.fail("whoami", "network_error", host="gitlab.com")
    res = run(store, restrict_repo=REPO)
    assert res.failures == [] and "gitlab.com" not in res.host_list.failed
    assert "gitlab.com" not in res.hosts or res.hosts["gitlab.com"].skipped is None
    assert not any(f.poll_exit for f in res.failures)


def test_restricted_poll_does_not_report_other_hosts_as_skipped(
    store: Store, fake: FakeForgectl
) -> None:
    add_query(store, "mine+open;host=github.com;host=nowhere.example.test")
    res = run(store, restrict_repo=REPO)
    assert "nowhere.example.test" not in res.hosts


def test_join_refuses_a_stored_row_of_another_host(store: Store) -> None:
    add_query(store)
    mine = pr(1)
    add_pr_watch(store, mine)
    foreign = en.Listed(
        mine["pr_id"],
        "gitlab.com/o/r#1",
        "gitlab.com/o/r",
        "gitlab.com",
        "2026-10-02T10:00:00.000000Z",
        "open",
    )
    assert en._join(store.conn, WATCH, foreign) is False
    assert source(store, mine) is None
    assert col(store, mine, "host") == HOST


def test_first_listing_start_before_year_one_is_left_to_the_full_pass(
    store: Store, fake: FakeForgectl
) -> None:
    # `first - overlap` is the operation that overflows; format_ts of a UTC value cannot.
    per_pr(store, 1, "0001-01-01T00:00:00.000000Z")
    res = run(store)
    assert fake.calls("prs") == [] and res.failures == []
