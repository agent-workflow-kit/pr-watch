"""`unreachable` and `recovered` through `poll` against the fake forgectl (#38)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pr_watch.state_dir import open_state_dir
from pr_watch.store import Store, open_store
from tests.forgectl_support import FakeForgectl
from tests.harness import assert_valid, schema_errors
from tests.test_hooks_poll import payloads, post_poll_config, write_config
from tests.test_hooks_poll import run as hrun
from tests.test_poll import (
    HOST,
    LATER,
    col,
    events,
    log_records,
    poll,
    pr,
    run,
    store_path,
    watch_pr,
)

T3, T4 = "2026-10-05T00:00:00Z", "2026-10-06T00:00:00Z"


@pytest.fixture(autouse=True)
def threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PR_WATCH_FAILURES__UNREACHABLE_AFTER", "2")


@pytest.fixture
def store(state_dir: Path) -> Iterator[Store]:
    with open_state_dir(state_dir) as state, open_store(state) as s:
        yield s


@pytest.fixture
def fake(fake_forgectl: FakeForgectl) -> FakeForgectl:
    return fake_forgectl.host(HOST, me="user-1")


def stored(store: Store) -> list[dict[str, Any]]:
    return [
        {"type": r[0], **json.loads(r[1])}
        for r in store.conn.execute("SELECT type, payload FROM events ORDER BY seq")
    ]


def fail_once(fake: FakeForgectl, ref: str) -> None:
    fake.not_found("snapshot", pr=ref, times=1)


def test_failures_crossing_the_threshold_make_one_unreachable_and_more_failures_none(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(1)
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    ref = snap["pr"]
    fail_once(fake, ref)
    _, doc = poll(capsys, "--pr", ref)
    assert events(store) == [] and doc["events"]["unreachable"] == 0
    fail_once(fake, ref)
    _, doc = poll(capsys, "--pr", ref)
    assert events(store) == ["unreachable"] and doc["events"]["unreachable"] == 1
    assert doc["prs"]["unreachable"] == 1 and col(store, snap["pr_id"], "status") == "unreachable"
    (event,) = stored(store)
    assert event["code"] == "not_found" and event["failures"] == 2
    assert event["head_sha"] == snap["head"]["sha"] and event["occurred_at"] is None
    assert event["repo"] == snap["repo"]
    for _ in range(3):  # an unreachable PR that keeps failing says nothing more
        fail_once(fake, ref)
        poll(capsys, "--pr", ref)
    assert events(store) == ["unreachable"]
    assert col(store, snap["pr_id"], "failure_count") == 5


def test_a_success_after_unreachable_is_one_recovered_and_failing_again_one_more_unreachable(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(2)
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    ref = snap["pr"]
    for _ in range(3):
        fail_once(fake, ref)
        poll(capsys, "--pr", ref)
    assert events(store) == ["unreachable"]
    _, doc = poll(capsys, "--pr", ref)  # the fake answers again
    assert doc["events"]["recovered"] == 1 and events(store) == ["unreachable", "recovered"]
    recovered = stored(store)[1]
    assert recovered["failures"] == 3 and recovered["code"] == "not_found"
    assert recovered["head_sha"] == snap["head"]["sha"] and recovered["occurred_at"] is None
    assert col(store, snap["pr_id"], "status") == "active"
    assert col(store, snap["pr_id"], "failure_count") == 0
    poll(capsys, "--pr", ref)  # a further success: no second recovered
    assert events(store) == ["unreachable", "recovered"]
    for _ in range(2):
        fail_once(fake, ref)
        poll(capsys, "--pr", ref)
    assert events(store) == ["unreachable", "recovered", "unreachable"]


def test_a_flap_below_the_threshold_is_no_event(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(3)
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    for _ in range(4):
        fail_once(fake, snap["pr"])
        poll(capsys, "--pr", snap["pr"])
        poll(capsys, "--pr", snap["pr"])  # success: the counter resets
    assert events(store) == [] and col(store, snap["pr_id"], "status") == "active"


def test_a_recovery_carries_the_changes_of_the_same_snapshot_after_it(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(4)
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    for _ in range(2):
        fail_once(fake, snap["pr"])
        poll(capsys, "--pr", snap["pr"])
    fake.update(snap["pr"], updated_at=LATER, head={"sha": "c" * 40})
    poll(capsys, "--pr", snap["pr"])
    assert events(store) == ["unreachable", "recovered", "pushed"]
    assert stored(store)[1]["head_sha"] == snap["head"]["sha"]  # the stored head, as unreachable


def test_a_recovery_that_shows_the_pr_closed_is_recovered_then_closed_never_inferred(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(5)
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    for _ in range(2):
        fail_once(fake, snap["pr"])
        poll(capsys, "--pr", snap["pr"])
    assert events(store) == ["unreachable"]  # not_found never closes the PR
    fake.update(snap["pr"], updated_at=LATER, state="closed", closed_at=LATER)
    poll(capsys, "--pr", snap["pr"])
    assert events(store) == ["unreachable", "recovered", "closed"]


def test_whole_host_failures_never_make_either_event(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(6)
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    fake.auth_required("snapshot")
    for _ in range(4):
        poll(capsys, "--pr", snap["pr"], want=None)
    assert events(store) == [] and col(store, snap["pr_id"], "failure_count") == 0


def test_a_row_without_a_digest_is_logged_not_stored_and_post_poll_still_names_it(
    fake: FakeForgectl,
    store: Store,
    tmp_path: Path,
    state_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snap = pr(7)
    fake.add(snap)
    code, out, _ = run(
        capsys, "--json", "watch", "add", snap["pr"], "--owner", "human:a", "--no-baseline"
    )
    assert code == 0, out
    fake.not_found("snapshot", pr=snap["pr"])
    body = tmp_path / "pp"
    cfg = post_poll_config(tmp_path, body)
    for _ in range(2):
        hrun(capsys, "poll", "--pr", snap["pr"], config=cfg)
    assert events(store) == [] and col(store, snap["pr_id"], "status") == "unreachable"
    (record,) = [r for r in log_records(state_dir) if r["event"] == "events_dropped"]
    assert record["reasons"] == ["invalid_head_sha"]
    payload = payloads(body)[-1]  # the poll before it failed the PR too, not yet unreachable
    named = {"pr": snap["pr"], "pr_id": snap["pr_id"], "code": "not_found"}
    assert payload["unreachable"] == [named]


def test_the_hooks_run_with_the_documented_payloads_and_are_capped_like_other_events(
    fake: FakeForgectl, store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(8)
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    out_u, out_r = tmp_path / "u", tmp_path / "r"
    cfg = write_config(
        tmp_path,
        "hooks:\n"
        f"  on-unreachable:\n    - cmd: 'cat >> {out_u}'\n"
        f"  on-recovered:\n    - cmd: 'cat >> {out_r}'\n",
    )
    doc: dict[str, Any] = {}
    for _ in range(2):
        fail_once(fake, snap["pr"])
        code, doc, _ = hrun(capsys, "poll", "--pr", snap["pr"], config=cfg)
        assert code == 0, doc
    assert doc["hooks"]["run"] == 1
    (u,) = payloads(out_u)
    assert_valid("hooks/on-unreachable", u)
    assert u["hook"] == "on-unreachable" and u["code"] == "not_found" and u["failures"] == 2
    _, doc, _ = hrun(capsys, "poll", "--pr", snap["pr"], config=cfg)
    assert doc["hooks"]["run"] == 1
    (r,) = payloads(out_r)
    assert_valid("hooks/on-recovered", r)
    assert r["type"] == "recovered" and r["failures"] == 2


def test_the_stored_events_validate_and_list_by_type(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(9)
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    for _ in range(2):
        fail_once(fake, snap["pr"])
        poll(capsys, "--pr", snap["pr"])
    poll(capsys, "--pr", snap["pr"])
    code, out, _ = run(capsys, "--json", "events", "--since", f"v1.{store.store_id}.0",
                       "--type", "unreachable", "--type", "recovered")  # fmt: skip
    assert code == 0
    got = json.loads(out)["events"]
    assert [e["type"] for e in got] == ["unreachable", "recovered"]
    assert all(schema_errors("event", e) == [] for e in got)
    code, out, _ = run(capsys, "events", "--since", f"v1.{store.store_id}.0")
    assert "not_found" in out and store_path(store)


def test_a_recovery_of_a_row_that_never_had_a_digest_is_dropped_not_orphaned(
    fake: FakeForgectl, store: Store, state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(10)
    fake.add(snap)
    code, out, _ = run(
        capsys, "--json", "watch", "add", snap["pr"], "--owner", "human:a", "--no-baseline"
    )
    assert code == 0, out
    for _ in range(2):
        fail_once(fake, snap["pr"])
        poll(capsys, "--pr", snap["pr"])
    assert col(store, snap["pr_id"], "status") == "unreachable" and events(store) == []
    poll(capsys, "--pr", snap["pr"])
    assert "recovered" not in events(store)
    assert col(store, snap["pr_id"], "status") == "active"
    reasons = [r["reasons"] for r in log_records(state_dir) if r["event"] == "events_dropped"]
    assert ["invalid_head_sha"] in reasons


def test_an_event_error_at_the_crossing_keeps_the_count_and_the_status(
    fake: FakeForgectl,
    store: Store,
    state_dir: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pr_watch.store import EventError

    snap = pr(11)
    fake.add(snap)
    watch_pr(capsys, snap["pr"])

    def boom(self: Store, *a: Any, **kw: Any) -> Any:
        raise EventError("forced")

    monkeypatch.setattr(Store, "append_event", boom)
    for _ in range(2):
        fail_once(fake, snap["pr"])
        poll(capsys, "--pr", snap["pr"])
    assert col(store, snap["pr_id"], "status") == "unreachable"
    assert col(store, snap["pr_id"], "failure_count") == 2 and events(store) == []
    assert any(r["event"] == "events_dropped" for r in log_records(state_dir))
