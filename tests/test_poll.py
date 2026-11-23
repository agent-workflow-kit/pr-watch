"""`poll`, through main() (and real processes where a signal or a race is the point), against the
fake forgectl (tests/fakebin/forgectl): scripted multi-cycle scenarios, the report, the exit codes,
signals, housekeeping (#25)."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pr_watch import poller
from pr_watch.cli import main
from pr_watch.leases import ProcFs
from pr_watch.state_dir import open_state_dir
from pr_watch.store import Store, format_ts, open_store
from tests.forgectl_support import FakeForgectl, new_pr, sha
from tests.harness import assert_valid

GOLDEN = Path(__file__).parent / "golden" / "poll"
HOST = "github.com"
GL = "gitlab.example.net"
OWNER = "human:a"
LATER = "2026-10-03T00:00:00Z"
LATEST = "2026-10-04T00:00:00Z"


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def poll(
    capsys: pytest.CaptureFixture[str], *args: str, want: int | None = 0
) -> tuple[int, dict[str, Any]]:
    """Run `poll --json`; the report is validated against its schema."""
    code, out, err = run(capsys, "--json", "poll", *args)
    if want is not None:
        assert code == want, (out, err)
    doc = json.loads(out)
    if "error" not in doc:
        assert_valid("cli/poll", doc)
    return code, doc


def pr(n: int, host: str = HOST, repo: str = "example-org/widgets", **ch: Any) -> dict[str, Any]:
    ch.setdefault("updated_at", "2026-10-02T10:30:00Z")
    return new_pr(host, repo, n, **ch)


@pytest.fixture
def store(state_dir: Path) -> Iterator[Store]:
    with open_state_dir(state_dir) as state, open_store(state) as s:
        yield s


@pytest.fixture
def fake(fake_forgectl: FakeForgectl) -> FakeForgectl:
    return fake_forgectl.host(HOST, me="user-1")


def watch_query(capsys: pytest.CaptureFixture[str], *extra: str) -> None:
    code, out, _ = run(
        capsys, "--json", "watch", "add", "--mine", "--open", "--owner", OWNER, *extra
    )
    assert code == 0, out


def watch_pr(capsys: pytest.CaptureFixture[str], ref: str) -> None:
    code, out, _ = run(capsys, "--json", "watch", "add", ref, "--owner", OWNER)
    assert code == 0, out


def events(store: Store) -> list[str]:
    return [str(r[0]) for r in store.conn.execute("SELECT type FROM events ORDER BY seq")]


def meta(store: Store, key: str) -> str | None:
    row = store.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def col(store: Store, pr_id: str, name: str) -> Any:
    row = store.conn.execute(f"SELECT {name} FROM prs WHERE pr_id = ?", (pr_id,)).fetchone()  # noqa: S608
    return None if row is None else row[0]


def host_row(store: Store, host: str = HOST) -> dict[str, Any]:
    cur = store.conn.execute("SELECT * FROM hosts WHERE host = ?", (host,))
    names = [d[0] for d in cur.description]
    row = cur.fetchone()
    return dict(zip(names, row, strict=True)) if row else {}


def log_records(state_dir: Path) -> list[dict[str, Any]]:
    path = state_dir / "log.jsonl"
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for record in records:
        assert_valid("log", record)
    return records


def calls(fake: FakeForgectl, command: str) -> int:
    return len(fake.calls(command))


def add_many(fake: FakeForgectl, count: int, **ch: Any) -> list[dict[str, Any]]:
    snaps = [pr(n, **ch) for n in range(1, count + 1)]
    for snap in snaps:
        fake.add(snap)
    return snaps


# --- first poll, quiet polls, changes ------------------------------------------------------------


def test_first_poll_with_many_members_baselines_them_silently(
    fake: FakeForgectl, store: Store, state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snaps = add_many(fake, 12)
    watch_query(capsys)
    _, doc = poll(capsys)
    assert doc["prs"]["snapshotted"] == 12 and doc["prs"]["unchanged"] == 12
    assert sum(doc["events"].values()) == 0 and doc["failed"] == []
    assert doc["hosts"][HOST]["queried"] and doc["hosts"][HOST]["snapshots"] == 12
    assert doc["hosts"][HOST]["requests_estimate"] == 12 * 7
    assert not doc["truncated"] and doc["deferred"] == 0 and not doc["already_running"]
    assert events(store) == []
    assert all(col(store, s["pr_id"], "digest") is not None for s in snaps)
    assert meta(store, "last_poll_finished_at") is not None  # unrestricted, exit 0, observed


def test_second_poll_with_nothing_changed_snapshots_nothing(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(fake, 3)
    watch_query(capsys)
    poll(capsys)
    before = calls(fake, "snapshot")
    _, doc = poll(capsys)
    assert doc["prs"]["snapshotted"] == 0 and calls(fake, "snapshot") == before
    assert calls(fake, "prs") == 2  # one listing per poll


def test_a_pushed_head_becomes_one_pushed_event_and_is_counted(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = add_many(fake, 2)[0]
    watch_query(capsys)
    poll(capsys)
    fake.update(snap["pr"], updated_at=LATER, head={"sha": sha(7)})
    _, doc = poll(capsys)
    assert doc["events"]["pushed"] == 1 and sum(doc["events"].values()) == 1
    assert doc["prs"]["snapshotted"] == 1 and doc["prs"]["unchanged"] == 0
    assert events(store) == ["pushed"]
    assert list(doc["events"]) == [
        "renamed", "opened", "reopened", "pushed", "retargeted", "comment", "review_comment",
        "review", "review_requested", "approved", "changes_requested", "approval_withdrawn",
        "ci", "ready", "converted_to_draft", "merged", "closed",
    ]  # fmt: skip


def test_a_pending_ci_storm_is_snapshotted_every_cycle_up_to_the_cap(
    fake: FakeForgectl,
    store: Store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_many(fake, 5, ci={"state": "pending"})
    watch_query(capsys)
    poll(capsys)  # baselines all five (no cap yet: 40)
    monkeypatch.setenv("PR_WATCH_POLL__MAX_SNAPSHOTS", "3")
    _, doc = poll(capsys)
    assert doc["prs"]["snapshotted"] == 3 and doc["truncated"] and doc["deferred"] == 2
    _, doc = poll(capsys)
    assert doc["prs"]["snapshotted"] == 3  # oldest last_polled_at first: the other two go first
    polled = {
        str(r[0])
        for r in store.conn.execute("SELECT pr_id FROM prs WHERE last_polled_at IS NOT NULL")
    }
    assert len(polled) == 5
    assert meta(store, "last_poll_finished_at") is not None  # a truncated poll still counts


def test_full_pass_spreads_over_cycles_and_ends_with_last_full_at(
    fake: FakeForgectl,
    store: Store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_many(fake, 5)
    watch_query(capsys)
    poll(capsys)  # baseline
    monkeypatch.setenv("PR_WATCH_POLL__FULL_EVERY", "1")
    monkeypatch.setenv("PR_WATCH_POLL__MAX_SNAPSHOTS", "2")
    _, first = poll(capsys)
    assert first["full_pass"] and first["prs"]["snapshotted"] == 2 and first["deferred"] == 3
    assert first["hosts"][HOST]["last_full_at"] is None  # still in progress
    assert host_row(store)["full_pass_started_at"] is not None
    _, second = poll(capsys)
    assert second["prs"]["snapshotted"] == 2 and second["truncated"]
    _, third = poll(capsys)
    assert third["prs"]["snapshotted"] == 1 and not third["truncated"]
    assert third["hosts"][HOST]["last_full_at"] is not None
    assert host_row(store)["full_pass_started_at"] is None


def test_full_flag_snapshots_every_active_pr_ignoring_the_cap(
    fake: FakeForgectl,
    store: Store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_many(fake, 6)
    watch_query(capsys)
    poll(capsys)
    monkeypatch.setenv("PR_WATCH_POLL__MAX_SNAPSHOTS", "2")
    _, doc = poll(capsys, "--full")
    assert doc["prs"]["snapshotted"] == 6 and doc["full_pass"] and not doc["truncated"]


# --- failures and pacing -------------------------------------------------------------------------


def test_a_rate_limit_pauses_the_host_and_the_next_poll_skips_it(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = add_many(fake, 2)[0]
    watch_query(capsys)
    poll(capsys)
    fake.update(snap["pr"], updated_at=LATER)
    fake.rate_limited("snapshot", retry_after=300)
    _, doc = poll(capsys)  # a rate limit is exit 0
    until = doc["hosts"][HOST]["paused_until"]
    assert until is not None and doc["failed"] == [] and doc["prs"]["snapshotted"] == 0
    assert col(store, snap["pr_id"], "failure_count") == 0
    assert host_row(store)["paused_until"] == until
    seen = (calls(fake, "snapshot"), calls(fake, "prs"))
    _, doc = poll(capsys)
    assert doc["hosts"][HOST]["paused_until"] == until and not doc["hosts"][HOST]["queried"]
    assert (calls(fake, "snapshot"), calls(fake, "prs")) == seen  # nothing was called


def test_a_pause_is_persisted_before_the_end_of_the_poll(
    fake: FakeForgectl,
    store: Store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 4: a killed or failing poll keeps the pause its host earned."""
    snap = add_many(fake, 1)[0]
    watch_query(capsys)
    poll(capsys)
    fake.update(snap["pr"], updated_at=LATER)
    fake.rate_limited("snapshot", retry_after=300)

    def boom(*args: Any, **kw: Any) -> Any:
        raise RuntimeError("the end of the poll was never reached")

    monkeypatch.setattr(poller.pc, "finish_hosts", boom)
    code, _, _ = run(capsys, "--json", "poll")
    assert code == 1
    assert host_row(store)["paused_until"] is not None


def test_an_auth_failure_fails_the_host_and_exits_12_with_the_report(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(fake, 2)
    watch_query(capsys)
    fake.auth_required("prs", host=HOST)
    code, out, err = run(capsys, "--json", "poll")
    assert code == 12
    doc = json.loads(out)
    assert_valid("cli/poll", doc)
    assert doc["failed"] == [{"kind": "host", "ref": HOST, "code": "auth_required"}]
    assert HOST in err and "auth_required" in err
    assert doc["hosts"][HOST]["errors"][0]["code"] == "auth_required"
    assert meta(store, "last_poll_finished_at") is None  # not exit 0


def test_an_auth_failure_on_one_host_does_not_stop_the_other(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.host(GL, me="user-1")
    fake.add(pr(1))
    fake.add(new_pr(GL, "example-org/widgets", 2, updated_at="2026-10-02T10:30:00Z"))
    watch_query(capsys)
    fake.auth_required("prs", host=HOST)
    code, doc = poll(capsys, want=12)
    assert doc["hosts"][GL]["snapshots"] == 1 and doc["hosts"][HOST]["snapshots"] == 0
    assert code == 12


def test_unreachable_prs_are_counted_once_and_reported_failed_each_cycle(
    fake: FakeForgectl,
    store: Store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PR_WATCH_FAILURES__UNREACHABLE_AFTER", "2")
    a, b = add_many(fake, 2)
    watch_query(capsys)
    poll(capsys)
    fake.update(a["pr"], updated_at=LATER)
    fake.not_found("snapshot", pr=a["pr"])
    _, one = poll(capsys)  # exit 0: a per-PR failure
    assert one["failed"] == [{"kind": "pr", "ref": a["pr"], "code": "not_found"}]
    assert one["prs"]["unreachable"] == 0 and col(store, a["pr_id"], "failure_count") == 1
    fake.update(a["pr"], updated_at=LATE_2)
    _, two = poll(capsys)
    assert two["prs"]["unreachable"] == 1 and col(store, a["pr_id"], "status") == "unreachable"
    assert col(store, b["pr_id"], "status") == "active"
    records = [r for r in log_records(store_path(store)) if r["event"] == "pr_status"]
    assert records and records[-1]["to"] == "unreachable"


LATE_2 = "2026-10-03T12:00:00Z"


def store_path(store: Store) -> Path:
    return Path(store.path).parent


def test_closed_and_reopened_prs_are_reactivated_with_one_reopened_event(
    fake: FakeForgectl, store: Store, state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(7)
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    fake.update(snap["pr"], state="closed", closed_at=LATER, updated_at=LATER)
    _, doc = poll(capsys)
    assert doc["events"]["closed"] == 1 and col(store, snap["pr_id"], "status") == "terminal"
    fake.update(snap["pr"], state="open", closed_at=None, updated_at=LATEST)
    _, doc = poll(capsys)
    assert col(store, snap["pr_id"], "status") == "active"
    assert doc["events"]["reopened"] == 1 and sum(doc["events"].values()) == 1
    assert events(store) == ["closed", "reopened"]
    statuses = [(r["from"], r["to"]) for r in log_records(state_dir) if r["event"] == "pr_status"]
    assert ("terminal", "active") in statuses


def test_a_renamed_repo_updates_the_stored_name_by_pr_id(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    old = pr(7)
    fake.add(old)
    watch_query(capsys)
    poll(capsys)
    fake.remove(old["pr"])
    renamed = pr(7, repo="example-org/gadgets", pr_id=old["pr_id"], updated_at=LATER)
    fake.add(renamed)
    _, doc = poll(capsys)
    assert col(store, old["pr_id"], "pr") == renamed["pr"]
    assert col(store, old["pr_id"], "repo") == renamed["repo"]
    assert doc["events"]["renamed"] == 1 and sum(doc["events"].values()) == 1
    assert events(store) == ["renamed"] and doc["failed"] == []


def test_a_closed_pr_is_not_snapshotted_again(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(7)
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    fake.update(snap["pr"], state="merged", merged_at=LATER, updated_at=LATER,
                merge_commit_sha=sha(3))  # fmt: skip
    poll(capsys)
    before = calls(fake, "snapshot")
    poll(capsys, "--full")
    assert calls(fake, "snapshot") == before


# --- host list: whoami ---------------------------------------------------------------------------


def stale_cache(store: Store, *hosts: str, age: timedelta = timedelta(hours=3)) -> None:
    doc = {
        "fetched_at": format_ts(datetime.now(UTC) - age),
        "hosts": [{"host": h, "backend": "github", "in_scope": True} for h in hosts],
    }
    with store.transaction() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES ('hosts_cache', ?)", (json.dumps(doc),))


def test_whoami_failure_with_a_stale_cache_goes_on_and_is_not_exit_12(
    fake: FakeForgectl, store: Store, state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(fake, 2)
    watch_query(capsys)
    stale_cache(store, HOST)
    fake.garbage("whoami")  # the whole call fails (a per-host failure is exit 12)
    code, out, err = run(capsys, "--json", "poll")
    assert code == 0
    doc = json.loads(out)
    assert doc["failed"] == [] and doc["prs"]["snapshotted"] == 2
    assert "whoami failed" in err and "cached host list" in err
    assert any(r["event"] == "host_failed" and r["host"] == HOST for r in log_records(state_dir))


def test_whoami_failure_with_no_cache_is_reported_and_exits_12(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(fake, 2)
    watch_query(capsys)
    fake.garbage("whoami")
    code, doc = poll(capsys, want=12)
    assert doc["failed"] == [{"kind": "host", "ref": "whoami", "code": "contract"}]
    assert calls(fake, "prs") == 0  # the query watches were skipped
    assert meta(store, "last_poll_finished_at") is None


def test_a_whole_whoami_rate_limit_pauses_every_known_host(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.host(GL, me="user-1")
    add_many(fake, 1)
    watch_query(capsys)
    stale_cache(store, HOST, GL)
    envelope = {"error": {"code": "rate_limited", "message": "slow down", "retry_after": 90}}
    fake.raw("whoami", stdout=json.dumps(envelope), exit=12)
    _, doc = poll(capsys)  # exit 0
    for host in (HOST, GL):
        assert host_row(store, host)["paused_until"] is not None
        assert doc["hosts"][host]["paused_until"] is not None
    assert doc["prs"]["snapshotted"] == 0
    snapshots = calls(fake, "snapshot")
    _, doc = poll(capsys)  # the next process honours it
    assert calls(fake, "snapshot") == snapshots and doc["hosts"][HOST]["paused_until"]


def test_a_whoami_rate_limit_pause_is_clamped_to_an_hour(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(fake, 1)
    watch_query(capsys)
    stale_cache(store, HOST)
    envelope = {"error": {"code": "rate_limited", "message": "slow down", "retry_after": 99999}}
    fake.raw("whoami", stdout=json.dumps(envelope), exit=12)
    poll(capsys)
    until = datetime.fromisoformat(str(host_row(store)["paused_until"]).replace("Z", "+00:00"))
    assert until <= datetime.now(UTC) + timedelta(hours=1, seconds=5)


def test_a_whole_whoami_rate_limit_lists_no_host_even_one_missing_from_the_cache(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.host(GL, me="user-1")
    add_many(fake, 1)
    other = new_pr(GL, "example-org/widgets", 2, updated_at="2026-10-02T10:30:00Z")
    fake.add(other)
    watch_query(capsys)
    watch_pr(capsys, other["pr"])  # a per-PR scope on a host the cache does not hold
    stale_cache(store, HOST)
    envelope = {"error": {"code": "rate_limited", "message": "slow down", "retry_after": 90}}
    fake.raw("whoami", stdout=json.dumps(envelope), exit=12)
    baselines = calls(fake, "snapshot")  # `watch add` baselined the per-PR watch
    _, doc = poll(capsys)
    assert calls(fake, "prs") == 0 and calls(fake, "snapshot") == baselines  # nothing was called
    assert doc["hosts"][GL]["paused_until"] is not None and not doc["hosts"][GL]["queried"]


def test_a_whoami_rate_limit_pauses_stored_rows_and_hosts_that_only_have_prs(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pause targets: the host list in use, the stored host rows, the hosts with PR rows."""
    fake.host(GL, me="user-1")
    add_many(fake, 1)
    other = new_pr(GL, "example-org/widgets", 2, updated_at="2026-10-02T10:30:00Z")
    fake.add(other)
    watch_query(capsys)
    watch_pr(capsys, other["pr"])  # GL: only a PR row, in neither the cache nor `hosts`
    with store.transaction() as conn:
        conn.execute("INSERT INTO hosts (host) VALUES ('stored.example.net')")
    stale_cache(store, HOST)
    envelope = {"error": {"code": "rate_limited", "message": "slow down", "retry_after": 90}}
    fake.raw("whoami", stdout=json.dumps(envelope), exit=12)
    poll(capsys)
    for host in (HOST, GL, "stored.example.net"):
        assert host_row(store, host)["paused_until"] is not None, host
    assert host_row(store, "never.example.net") == {}


def test_a_whoami_config_invalid_with_a_stale_cache_is_named_in_failed(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(fake, 1)
    watch_query(capsys)
    stale_cache(store, HOST)
    envelope = {
        "error": {"code": "config_invalid", "message": "bad", "details": {"key": "hosts.x"}}
    }
    fake.raw("whoami", stdout=json.dumps(envelope), exit=2)  # the call as a whole
    baselines = calls(fake, "snapshot")
    code, out, err = run(capsys, "--json", "poll")
    doc = json.loads(out)
    assert code == 12 and calls(fake, "snapshot") == baselines
    assert doc["failed"] == [{"kind": "host", "ref": "whoami", "code": "config_invalid"}]
    assert "cached host list is reused" not in err and "whoami" in err


def test_forgectl_config_invalid_on_a_host_call_stops_the_poll_with_exit_12(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(fake, 2)
    watch_query(capsys)
    fake.fail("prs", "config_invalid", details={"key": "hosts.x"}, host=HOST)
    code, doc = poll(capsys, want=12)
    assert doc["prs"]["snapshotted"] == 0 and doc["failed"][0]["code"] == "config_invalid"
    assert calls(fake, "snapshot") == 0


# --- budgets and config --------------------------------------------------------------------------


def test_max_duration_stops_new_snapshots_and_reports_truncated(
    fake: FakeForgectl,
    store: Store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_many(fake, 3)
    watch_query(capsys)
    monkeypatch.setenv("PR_WATCH_POLL__MAX_DURATION", "1ms")  # spent by enumeration alone
    _, doc = poll(capsys)
    assert doc["truncated"] and doc["deferred"] == 3 and doc["prs"]["snapshotted"] == 0
    assert calls(fake, "snapshot") == 0


def test_a_cut_listing_then_max_duration_snapshots_the_cut_prs_next_cycle(
    fake: FakeForgectl,
    store: Store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance criterion 3: an incomplete listing and a spent budget lose nothing."""
    snaps = add_many(fake, 3)
    watch_query(capsys)
    cut = [True]
    real = poller.ForgectlClient.prs

    def prs(self: Any, **kw: Any) -> Any:
        out = real(self, **kw)
        if cut[0]:
            assert out.result is not None
            out.result["complete"] = False
        return out

    monkeypatch.setattr(poller.ForgectlClient, "prs", prs)
    monkeypatch.setenv("PR_WATCH_POLL__MAX_DURATION", "1ms")
    _, doc = poll(capsys)
    assert doc["hosts"][HOST]["incomplete_listing"] and doc["truncated"]
    assert doc["deferred"] == 3 and doc["prs"]["snapshotted"] == 0
    assert all(col(store, s["pr_id"], "digest") is None for s in snaps)  # still unbaselined
    cut[0] = False
    monkeypatch.delenv("PR_WATCH_POLL__MAX_DURATION")
    _, doc = poll(capsys)
    assert doc["prs"]["snapshotted"] == 3 and not doc["truncated"] and doc["deferred"] == 0
    assert all(col(store, s["pr_id"], "digest") is not None for s in snaps)


def test_a_host_with_only_per_pr_watches_learns_its_backend_from_the_snapshot(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    host = "ghe.example.net"  # not github.com, and never asked by whoami (no query watch)
    fake.host(host, backend="github", me="user-1")
    snap = new_pr(
        host, "example-org/widgets", 5, backend="github", updated_at="2026-10-02T10:30:00Z"
    )
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    fake.update(snap["pr"], updated_at=LATER)
    _, doc = poll(capsys)
    assert calls(fake, "whoami") == 0 and doc["hosts"][host]["snapshots"] == 1
    assert host_row(store, host)["backend"] == "github"
    assert doc["hosts"][host]["requests_estimate"] == 7  # not the GitLab 21 of an unknown host


@pytest.mark.parametrize("value", ["0", "0s", "0ms"])
def test_a_non_positive_max_duration_is_config_invalid_naming_the_key(
    value: str,
    fake: FakeForgectl,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PR_WATCH_POLL__MAX_DURATION", value)
    code, out, _ = run(capsys, "--json", "poll")
    error = json.loads(out)["error"]
    assert code == 2 and error["code"] == "config_invalid"
    assert error["details"]["key"] == "poll.max_duration"


def test_forgectl_missing_is_the_tool_missing_envelope_before_any_work(
    store: Store,
    state_dir: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    code, out, _ = run(capsys, "--json", "poll")
    error = json.loads(out)["error"]
    assert code == 12 and error["code"] == "tool_missing" and "install" in error["details"]
    assert [r for r in log_records(state_dir) if r["event"] in ("poll_start", "poll_end")] == []


# --- the log -------------------------------------------------------------------------------------


def test_poll_start_names_forgectl_once_and_poll_end_summarises(
    fake: FakeForgectl, store: Store, state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(fake, 2)
    watch_query(capsys)
    poll(capsys)
    records = log_records(state_dir)
    starts = [r for r in records if r["event"] == "poll_start"]
    assert len(starts) == 1
    assert Path(starts[0]["forgectl_path"]).is_absolute()
    assert starts[0]["forgectl_version"].startswith("0.") and starts[0]["full"] is False
    ends = [r for r in records if r["event"] == "poll_end"]
    assert len(ends) == 1
    assert (ends[0]["snapshots"], ends[0]["events"], ends[0]["errors"]) == (2, 0, 0)
    assert ends[0]["interrupted"] is False and ends[0]["already_running"] is False
    summary = [r for r in records if r["event"] == "host_summary"]
    assert [(r["host"], r["snapshots"]) for r in summary] == [(HOST, 2)]


# --- restricted polls ----------------------------------------------------------------------------


def test_pr_poll_refreshes_one_pr_and_leaves_last_poll_finished_at(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    a, b = add_many(fake, 2)
    watch_query(capsys)
    poll(capsys)
    stamp = meta(store, "last_poll_finished_at")
    fake.update(a["pr"], head={"sha": sha(9)})  # no updated_at change: only --pr sees it
    time.sleep(0.01)
    _, doc = poll(capsys, "--pr", a["pr"])
    assert doc["polled"] is True and doc["events"]["pushed"] == 1
    assert list(doc["hosts"]) == [HOST] and not doc["hosts"][HOST]["queried"]
    assert calls(fake, "prs") == 1  # the first poll's listing only: --pr lists nothing
    assert meta(store, "last_poll_finished_at") == stamp
    assert col(store, b["pr_id"], "rev") == 1


def test_pr_poll_of_an_unknown_pr_is_not_found(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, "--json", "poll", "--pr", f"{HOST}/example-org/widgets#99")
    assert code == 10 and json.loads(out)["error"]["code"] == "not_found"
    assert calls(fake, "snapshot") == 0


def test_pr_poll_of_a_merged_pr_is_polled_false(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(7)
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    fake.update(snap["pr"], state="merged", merged_at=LATER, updated_at=LATER,
                merge_commit_sha=sha(3))  # fmt: skip
    poll(capsys)
    before = calls(fake, "snapshot")
    _, doc = poll(capsys, "--pr", snap["pr"])
    assert doc["polled"] is False and calls(fake, "snapshot") == before


def test_pr_poll_reactivates_a_closed_row_that_is_open_again(
    fake: FakeForgectl, store: Store, state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(7)
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    fake.update(snap["pr"], state="closed", closed_at=LATER, updated_at=LATER)
    poll(capsys)
    fake.update(snap["pr"], state="open", closed_at=None, updated_at=LATEST)
    _, doc = poll(capsys, "--pr", snap["pr"])
    assert doc["polled"] is True and col(store, snap["pr_id"], "status") == "active"
    assert doc["events"]["reopened"] == 1 and sum(doc["events"].values()) == 1
    assert events(store) == ["closed", "reopened"]
    assert ("terminal", "active") in [
        (r["from"], r["to"]) for r in log_records(state_dir) if r["event"] == "pr_status"
    ]


def test_pr_poll_under_a_host_pause_is_polled_false(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = add_many(fake, 1)[0]
    watch_query(capsys)
    poll(capsys)
    fake.update(snap["pr"], updated_at=LATER)
    fake.rate_limited("snapshot", retry_after=300)
    poll(capsys)
    before = calls(fake, "snapshot")
    _, doc = poll(capsys, "--pr", snap["pr"])
    assert doc["polled"] is False and doc["hosts"][HOST]["paused_until"] is not None
    assert calls(fake, "snapshot") == before


def test_repo_poll_reports_only_its_host_and_never_sets_last_poll_finished_at(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.host(GL, me="user-1")
    fake.add(pr(1))
    fake.add(new_pr(GL, "example-org/widgets", 2, updated_at="2026-10-02T10:30:00Z"))
    watch_query(capsys)
    _, doc = poll(capsys, "--repo", f"{HOST}/example-org/widgets")
    assert list(doc["hosts"]) == [HOST] and doc["prs"]["snapshotted"] == 1
    assert meta(store, "last_poll_finished_at") is None
    assert host_row(store, HOST)["poll_count_since_full"] == 0  # a restricted poll never counts


# --- housekeeping --------------------------------------------------------------------------------


def test_prune_runs_once_a_day_at_the_end_of_a_poll(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(fake, 1)
    watch_query(capsys)
    assert meta(store, "last_prune_at") is None
    poll(capsys)
    first = meta(store, "last_prune_at")
    assert first is not None
    poll(capsys)
    assert meta(store, "last_prune_at") == first  # less than a day: not again
    old = format_ts(datetime.now(UTC) - timedelta(days=2))
    with store.transaction() as conn:
        conn.execute("UPDATE meta SET value = ? WHERE key = 'last_prune_at'", (old,))
    poll(capsys)
    assert meta(store, "last_prune_at") not in (None, old)


def test_prune_also_runs_after_a_poll_that_exits_12(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(fake, 1)
    watch_query(capsys)
    fake.garbage("whoami")
    poll(capsys, want=12)
    assert meta(store, "last_prune_at") is not None  # an exit 12 poll still ran to its end


@pytest.mark.parametrize("extra", [(), ("--full",)])
def test_already_running_reports_no_deferred_truncated_or_full_pass(
    extra: tuple[str, ...],
    fake: FakeForgectl,
    store: Store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snaps = add_many(fake, 3)
    watch_query(capsys)
    poll(capsys)
    for snap in snaps:
        fake.update(snap["pr"], updated_at=LATER)
        other = ProcFs().identity(1)  # a live poll of another process holds every lease
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO leases VALUES (?, ?, ?, ?, ?)",
                (snap["pr_id"], other.pid, other.start, other.boot_id,
                 format_ts(datetime.now(UTC) + timedelta(minutes=30))),
            )  # fmt: skip
    monkeypatch.setenv("PR_WATCH_POLL__MAX_SNAPSHOTS", "1")  # would defer two
    _, doc = poll(capsys, *extra)
    assert doc["already_running"] is True and doc["prs"]["skipped_locked"] >= 1
    assert doc["deferred"] == 0 and doc["truncated"] is False and doc["full_pass"] is False
    assert doc["prs"]["snapshotted"] == 0 and doc["prs"]["incomplete"] == 0


@pytest.mark.parametrize("failure", [sqlite3.OperationalError("disk I/O error"), OSError("EIO")])
def test_a_housekeeping_failure_is_a_stderr_warning_and_one_json_document(
    failure: Exception,
    fake: FakeForgectl,
    store: Store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_many(fake, 1)
    watch_query(capsys)

    def boom(*args: Any, **kw: Any) -> Any:
        raise failure

    monkeypatch.setattr(poller.prune, "prune", boom)
    code, out, err = run(capsys, "--json", "poll")
    assert code == 0 and "housekeeping" in err
    assert json.loads(out)["prs"]["snapshotted"] == 1  # exactly one document


def signal_in_process(monkeypatch: pytest.MonkeyPatch, owner: Any, name: str, after: bool) -> None:
    """Deliver SIGTERM to this process from inside `owner.name` (the poll's handler is live)."""
    real = getattr(owner, name)

    def wrapper(*args: Any, **kw: Any) -> Any:
        if not after:
            signal.raise_signal(signal.SIGTERM)
        result = real(*args, **kw)
        if after:
            signal.raise_signal(signal.SIGTERM)
        return result

    monkeypatch.setattr(owner, name, wrapper)


def test_a_signal_in_finish_hosts_exits_143_with_no_report_and_no_finished_stamp(
    fake: FakeForgectl,
    store: Store,
    state_dir: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_many(fake, 1)
    watch_query(capsys)
    signal_in_process(monkeypatch, poller.pc, "finish_hosts", after=True)
    code, out, err = run(capsys, "--json", "poll")
    assert code == 128 + signal.SIGTERM and out == "" and "Traceback" not in err
    assert meta(store, "last_poll_finished_at") is None and meta(store, "last_prune_at") is None
    ends = [r for r in log_records(state_dir) if r["event"] == "poll_end"]
    assert len(ends) == 1 and ends[0]["interrupted"] is True


def test_a_store_error_persisting_the_interrupted_pause_still_exits_143_with_a_warning(
    fake: FakeForgectl,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_many(fake, 1)
    watch_query(capsys)
    fake.rate_limited("prs", retry_after=300, host=HOST)
    signal_in_process(monkeypatch, poller, "_enumerate", after=True)

    def boom(*args: Any, **kw: Any) -> Any:
        raise poller.StoreError("disk full")

    monkeypatch.setattr(poller.pc, "finish_host", boom)
    code, out, err = run(capsys, "--json", "poll")
    assert code == 128 + signal.SIGTERM and out == ""
    assert "warning: could not persist a rate-limit pause" in err and "Traceback" not in err


def test_a_signal_in_finish_poll_exits_143_with_no_report(
    fake: FakeForgectl,
    store: Store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_many(fake, 1)
    watch_query(capsys)
    signal_in_process(monkeypatch, poller.pc, "finish_poll", after=True)
    code, out, _ = run(capsys, "--json", "poll")
    assert code == 128 + signal.SIGTERM and out == ""
    assert meta(store, "last_prune_at") is None


def test_a_signal_after_the_report_skips_housekeeping_and_exits_143(
    fake: FakeForgectl,
    store: Store,
    state_dir: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_many(fake, 1)
    watch_query(capsys)
    signal_in_process(monkeypatch, poller, "_write_end", after=True)  # runs after the emit
    code, out, _ = run(capsys, "--json", "poll")
    assert code == 128 + signal.SIGTERM
    assert json.loads(out)["prs"]["snapshotted"] == 1  # the report was already out
    assert meta(store, "last_prune_at") is None  # and no prune started


def test_a_signal_during_the_prune_stops_it_unstamped_and_exits_143(
    fake: FakeForgectl,
    store: Store,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_many(fake, 1)
    watch_query(capsys)
    seen: list[bool] = []
    real = poller.prune.prune

    def prune(*args: Any, should_stop: Any = None, **kw: Any) -> Any:
        signal.raise_signal(signal.SIGTERM)  # arrives as the prune begins
        seen.append(should_stop is not None and should_stop())
        return real(*args, should_stop=should_stop, **kw)

    monkeypatch.setattr(poller.prune, "prune", prune)
    code, _, _ = run(capsys, "--json", "poll")
    assert code == 128 + signal.SIGTERM and seen == [True]
    assert meta(store, "last_prune_at") is None


# --- output --------------------------------------------------------------------------------------


def test_human_output_summarises_and_quiet_prints_nothing(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(fake, 2)
    watch_query(capsys)
    code, out, _ = run(capsys, "poll")
    assert code == 0 and "polled 2 PRs" in out and f"{HOST}: 2 snapshots" in out
    code, out, _ = run(capsys, "--quiet", "poll")
    assert (code, out) == (0, "")


def test_human_output_lists_the_failures(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    add_many(fake, 1)
    watch_query(capsys)
    fake.auth_required("prs", host=HOST)
    code, out, _ = run(capsys, "poll")
    assert code == 12 and "failed:" in out and "auth_required" in out


GOLDEN_RUNS = ("first-poll", "pushed", "failed")


def _norm(doc: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(doc))
    out["started_at"] = "TS"
    out["duration_ms"] = 0
    for host in out["hosts"].values():
        for key in ("paused_until", "last_full_at"):
            if host[key] is not None:
                host[key] = "TS"
    return out


@pytest.mark.parametrize("name", GOLDEN_RUNS)
def test_golden_reports(
    name: str, fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snaps = add_many(fake, 3)
    watch_query(capsys)
    if name == "first-poll":
        _, doc = poll(capsys)
    elif name == "pushed":
        poll(capsys)
        fake.update(snaps[0]["pr"], updated_at=LATER, head={"sha": sha(7)})
        _, doc = poll(capsys)
    else:
        fake.auth_required("prs", host=HOST)
        _, doc = poll(capsys, want=12)
    assert _norm(doc) == json.loads((GOLDEN / f"{name}.json").read_text())


# --- processes: concurrency and signals ----------------------------------------------------------


def spawn(*argv: str, env: dict[str, str] | None = None) -> subprocess.Popen[str]:
    env = {**(os.environ if env is None else env), "PYTHONPATH": os.pathsep.join(sys.path)}
    return subprocess.Popen(
        [sys.executable, "-m", "pr_watch", "--json", *argv],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True,
    )  # fmt: skip


def wait_for_call(fake: FakeForgectl, command: str, timeout: float = 30.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if fake.calls(command):
            return
        time.sleep(0.05)
    raise AssertionError(f"forgectl {command} was never called")


def finish(proc: subprocess.Popen[str], timeout: float = 120.0) -> tuple[int, str, str]:
    """Wait for a process this test started; kill exactly that one if it overruns."""
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    return proc.returncode, out, err


def snapshots_overlapped(fake: FakeForgectl) -> bool:
    """Two `snapshot` calls were both started before either ended (the order of the call log)."""
    lines = [
        json.loads(line)
        for line in (fake.dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    ids = [r["id"] for r in lines if r["phase"] == "start" and "snapshot" in r["argv"]]
    if len(ids) != 2:
        return False
    started = [i for i, r in enumerate(lines) if r["phase"] == "start" and r["id"] in ids]
    ended = [i for i, r in enumerate(lines) if r["phase"] == "end" and r["id"] in ids]
    return len(ended) == 2 and max(started) < min(ended)


def test_two_concurrent_polls_the_second_reports_already_running(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = add_many(fake, 1)[0]
    watch_query(capsys)
    fake.slow("snapshot", 4.0)
    first = spawn("poll")
    try:
        wait_for_call(fake, "snapshot")
        code, out, err = finish(spawn("poll"))
        assert code == 0, err
        doc = json.loads(out)
        assert_valid("cli/poll", doc)
        assert doc["already_running"] is True and doc["prs"]["skipped_locked"] == 1
        assert doc["prs"]["snapshotted"] == 0 and sum(doc["events"].values()) == 0
        assert meta(store, "last_poll_finished_at") is None  # neither poll has finished yet
    finally:
        code, out, err = finish(first)
    assert code == 0, err
    assert json.loads(out)["prs"]["snapshotted"] == 1
    assert col(store, snap["pr_id"], "rev") == 1
    assert store.conn.execute("SELECT count(*) FROM leases").fetchone()[0] == 0


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_a_stop_signal_mid_poll_exits_128_plus_it_with_nothing_partial_and_no_leases(
    signum: signal.Signals,
    fake: FakeForgectl,
    store: Store,
    state_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snap = add_many(fake, 2)[0]
    watch_query(capsys)
    fake.slow("snapshot", 60.0)
    proc = spawn("poll")
    wait_for_call(fake, "snapshot")
    proc.send_signal(signum)  # the exact process this test started
    code, out, err = finish(proc, timeout=60)
    assert code == 128 + signum and out == ""
    assert "Traceback" not in err
    assert events(store) == [] and col(store, snap["pr_id"], "digest") is None
    assert store.conn.execute("SELECT count(*) FROM leases").fetchone()[0] == 0
    assert meta(store, "last_poll_finished_at") is None
    ends = [r for r in log_records(state_dir) if r["event"] == "poll_end"]
    assert len(ends) == 1 and ends[0]["interrupted"] is True
    for call in fake.calls("snapshot"):  # no forgectl child of the poll was left running
        assert call.exit is not None or not Path(f"/proc/{call.pid}").exists()


def test_a_concurrent_watch_add_and_poll_store_one_baseline(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """The add + poll variant of the race (#18): both fetch the new PR before either writes."""
    snap = pr(7)
    fake.add(snap)
    watch_query(capsys)
    fake.slow("snapshot", 10.0)  # both processes must be inside their snapshot call together
    procs = [
        spawn("poll"),
        spawn("watch", "add", snap["pr"], "--owner", "agent:b"),
    ]
    results = [finish(p) for p in procs]
    for code, _, err in results:
        assert code == 0, err
    assert snapshots_overlapped(fake), "the two fetches never overlapped: the race was not run"
    assert store.conn.execute("SELECT count(*) FROM prs").fetchone()[0] == 1
    assert col(store, snap["pr_id"], "rev") == 1 and col(store, snap["pr_id"], "digest")
    assert events(store) == []


def test_a_signal_during_the_version_preflight_exits_with_the_signal_not_tool_missing(
    store: Store, tmp_path: Path
) -> None:
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    ready = stub_dir / "ready"
    stub = stub_dir / "forgectl"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        f"open({str(ready)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    env = {**os.environ, "PATH": os.pathsep.join([str(stub_dir), os.environ["PATH"]])}
    proc = spawn("poll", env=env)
    try:
        end = time.monotonic() + 60
        while not ready.exists() and time.monotonic() < end:
            time.sleep(0.05)
        assert ready.exists(), "the stub forgectl never started"
        stub_pid = int(ready.read_text())
        proc.send_signal(signal.SIGTERM)  # the exact process this test started
        code, out, err = finish(proc, timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert code == 128 + signal.SIGTERM, (out, err)
    assert out == "" and "tool_missing" not in err and "Traceback" not in err
    end = time.monotonic() + 15
    while Path(f"/proc/{stub_pid}").exists() and time.monotonic() < end:
        time.sleep(0.05)
    state = Path(f"/proc/{stub_pid}/stat")
    assert not state.exists() or state.read_text().rsplit(")", 1)[1].split()[0] in ("Z", "X")


def test_a_signal_in_enumeration_keeps_the_pause_learned_and_counts_no_failure(
    fake: FakeForgectl, store: Store, state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.host(GL, me="user-1")
    fake.add(pr(1))
    fake.add(new_pr(GL, "example-org/widgets", 2, updated_at="2026-10-02T10:30:00Z"))
    watch_query(capsys)
    fake.rate_limited("prs", retry_after=300, host=HOST)  # listed first (host order)
    fake.slow("prs", 60.0, host=GL)
    proc = spawn("poll")
    end = time.monotonic() + 60
    while len(fake.calls("prs")) < 2 and time.monotonic() < end:  # the GL listing has begun
        time.sleep(0.05)
    assert len(fake.calls("prs")) == 2
    proc.send_signal(signal.SIGTERM)  # the exact process this test started
    code, out, err = finish(proc, timeout=60)
    assert code == 128 + signal.SIGTERM and out == "", err
    assert host_row(store, HOST)["paused_until"] is not None  # the pause survived the signal
    records = log_records(state_dir)
    ends = [r for r in records if r["event"] == "poll_end"]
    assert len(ends) == 1 and ends[0]["interrupted"] is True and ends[0]["errors"] == 0
    assert not [r for r in records if r["event"] == "host_failed" and r["host"] == GL]
