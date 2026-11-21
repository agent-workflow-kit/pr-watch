"""`opened` and `reopened` through `poll` against the fake forgectl (#34): multi-poll sequences."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pr_watch.state_dir import open_state_dir
from pr_watch.store import Store, open_store
from tests.forgectl_support import FakeForgectl, sha
from tests.harness import assert_valid, schema_errors
from tests.test_poll import (
    HOST,
    LATER,
    LATEST,
    col,
    events,
    poll,
    pr,
    run,
    watch_pr,
    watch_query,
)

FUTURE = "2099-01-01T00:00:00Z"  # after any watch created while the suite runs
OLD = "2026-10-01T09:00:00Z"


@pytest.fixture
def store(state_dir: Path) -> Iterator[Store]:
    with open_state_dir(state_dir) as state, open_store(state) as s:
        yield s


@pytest.fixture
def fake(fake_forgectl: FakeForgectl) -> FakeForgectl:
    return fake_forgectl.host(HOST, me="user-1")


def cursor(store: Store) -> str:
    return f"v1.{store.store_id}.0"


def stored(store: Store) -> list[dict[str, Any]]:
    return [
        {"type": r[0], "pr": r[1], "payload": json.loads(r[2])}
        for r in store.conn.execute("SELECT type, pr, payload FROM events ORDER BY seq")
    ]


def test_first_enumeration_emits_no_opened_even_for_a_fresh_pr(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.add(pr(1, created_at=OLD))
    watch_query(capsys)
    _, doc = poll(capsys)
    assert sum(doc["events"].values()) == 0 and events(store) == []


def test_a_pr_created_after_the_watch_emits_one_opened_then_baselines(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.add(pr(1, created_at=OLD))
    watch_query(capsys)
    poll(capsys)
    new = pr(2, created_at=FUTURE)
    fake.add(new)
    _, doc = poll(capsys)
    assert doc["events"]["opened"] == 1 and events(store) == ["opened"]
    (event,) = stored(store)
    assert event["pr"] == new["pr"] and event["payload"]["head_sha"] == new["head"]["sha"]
    assert event["payload"]["occurred_at"] == "2099-01-01T00:00:00.000000Z"
    # the baseline is silent and a later poll repeats nothing
    fake.update(new["pr"], updated_at=LATEST)
    _, doc = poll(capsys)
    assert sum(doc["events"].values()) == 0 and events(store) == ["opened"]
    _, doc = poll(capsys)
    assert events(store) == ["opened"]


def test_opened_payload_validates_and_names_the_watch(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_query(capsys)
    poll(capsys)
    fake.add(pr(2, created_at=FUTURE))
    poll(capsys)
    code, out, _ = run(capsys, "--json", "events", "--since", cursor(store))
    assert code == 0
    (event,) = json.loads(out)["events"]
    assert schema_errors("event", event) == [] and event["type"] == "opened"
    assert len(event["watches"]) == 1


def test_an_old_pr_that_newly_matches_the_watch_emits_no_opened(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_query(capsys, "--repo", f"{HOST}/example-org/widgets")
    poll(capsys)
    # an older PR of the account appears in the watch later (its repo filter now matches)
    fake.add(pr(3, created_at=OLD))
    _, doc = poll(capsys)
    assert doc["prs"]["snapshotted"] == 1 and sum(doc["events"].values()) == 0
    assert events(store) == []


def watch_pr_unbaselined(capsys: pytest.CaptureFixture[str], ref: str) -> None:
    """Name `ref` with no first snapshot, so that poll takes it (it is also a query member)."""
    code, out, _ = run(capsys, "--json", "watch", "add", ref, "--owner", "human:a", "--no-baseline")
    assert code == 0, out


def test_a_per_pr_watch_never_gets_opened(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_query(capsys)
    poll(capsys)
    snap = pr(5, created_at=FUTURE)
    fake.add(snap)
    # the query watch lists PR 5 as a new member, and a per-PR watch also names it; the first
    # snapshot happens in the poll below, where only the per-PR exclusion keeps `opened` out
    watch_pr_unbaselined(capsys, snap["pr"])
    _, doc = poll(capsys)
    assert doc["prs"]["snapshotted"] == 1 and sum(doc["events"].values()) == 0
    assert events(store) == []


def test_a_member_named_by_a_per_pr_watch_before_its_first_snapshot_gets_no_opened(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_query(capsys)
    poll(capsys)
    snap = pr(6, created_at=FUTURE)
    fake.add(snap)
    watch_pr_unbaselined(capsys, snap["pr"])
    fake.update(snap["pr"], updated_at=LATEST)
    poll(capsys)
    assert events(store) == []


def test_a_pr_named_by_a_per_pr_watch_without_a_baseline_gets_no_opened(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(6, created_at=FUTURE)
    fake.add(snap)
    code, out, _ = run(
        capsys, "--json", "watch", "add", snap["pr"], "--owner", "human:a", "--no-baseline"
    )
    assert code == 0, out
    watch_query(capsys)
    poll(capsys)  # the first snapshot of a PR that a per-PR watch names
    assert events(store) == []


def test_reopened_from_closed_emits_one_reopened_and_pushed_when_the_head_moved(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(7)
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    fake.update(snap["pr"], state="closed", closed_at=LATER, updated_at=LATER)
    poll(capsys)
    fake.update(snap["pr"], state="open", closed_at=None, updated_at=LATEST, head={"sha": sha(9)})
    _, doc = poll(capsys)
    assert doc["events"]["reopened"] == 1 and doc["events"]["pushed"] == 1
    assert events(store) == ["closed", "reopened", "pushed"]
    _, doc = poll(capsys)
    assert events(store) == ["closed", "reopened", "pushed"]


def test_reopened_without_a_head_change_is_alone(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(7)
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    fake.update(snap["pr"], state="closed", closed_at=LATER, updated_at=LATER)
    poll(capsys)
    fake.update(snap["pr"], state="open", closed_at=None, updated_at=LATEST)
    poll(capsys, "--pr", snap["pr"])
    assert events(store) == ["closed", "reopened"]
    (event,) = [e for e in stored(store) if e["type"] == "reopened"]
    assert event["payload"]["occurred_at"] is None
    assert event["payload"]["head_sha"] == snap["head"]["sha"]


def test_a_merged_pr_is_never_reopened(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(7)
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    fake.update(snap["pr"], state="merged", merged_at=LATER, updated_at=LATER)
    poll(capsys)
    fake.update(snap["pr"], state="open", merged_at=None, updated_at=LATEST)
    poll(capsys)
    assert events(store) == ["merged"]


def test_a_query_member_that_closes_and_reopens_emits_reopened_not_opened(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_query(capsys)
    poll(capsys)
    snap = pr(2, created_at=FUTURE)
    fake.add(snap)
    poll(capsys)
    fake.update(snap["pr"], state="closed", closed_at=LATER, updated_at=LATER)
    poll(capsys, "--pr", snap["pr"])
    fake.update(snap["pr"], state="open", closed_at=None, updated_at=LATEST)
    poll(capsys)
    assert events(store) == ["opened", "closed", "reopened"]


def test_the_report_counts_and_the_poll_schema_accept_the_new_types(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_query(capsys)
    poll(capsys)
    fake.add(pr(2, created_at=FUTURE))
    _, doc = poll(capsys)
    assert_valid("cli/poll", doc)
    assert doc["events"]["opened"] == 1 and sum(doc["events"].values()) == 1


def test_two_query_watches_use_the_oldest_watch_time_for_opened(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_query(capsys)
    watch_query(capsys, "--repo", f"{HOST}/example-org/widgets")
    old, new = (r[0] for r in store.conn.execute("SELECT id FROM watches ORDER BY created_at, id"))
    store.conn.execute(
        "UPDATE watches SET created_at = ? WHERE id = ?", ("2026-10-01T00:00:00.000000Z", old)
    )
    store.conn.execute(
        "UPDATE watches SET created_at = ? WHERE id = ?", ("2026-10-03T00:00:00.000000Z", new)
    )
    store.conn.commit()
    poll(capsys)
    # created between the two watches: after the older, before the newer; both list it
    fake.add(pr(2, created_at="2026-10-02T00:00:00Z"))
    _, doc = poll(capsys)
    assert doc["events"]["opened"] == 1 and events(store) == ["opened"]


def test_a_new_member_already_merged_at_first_sight_emits_opened_then_merged(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_query(capsys)
    poll(capsys)
    snap = pr(2, created_at=FUTURE, state="merged", merged_at=LATER, updated_at=LATER)
    # a stale index still lists it open, so it joins as a member and its first snapshot is merged
    fake.add(snap, listing={"state": "open"})
    _, doc = poll(capsys)
    assert doc["events"]["opened"] == 1 and doc["events"]["merged"] == 1
    assert events(store) == ["opened", "merged"]
    assert col(store, snap["pr_id"], "status") == "terminal"


def drop_row(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str], snap: dict[str, Any]
) -> None:
    """Make the PR leave the listing while its snapshot says open, until the row is deleted."""
    fake.add(snap, listing={"state": "closed"})
    for _ in range(4):
        poll(capsys)
    assert col(store, snap["pr_id"], "status") is None


def test_a_member_that_leaves_and_rejoins_gets_one_opened(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_query(capsys)
    poll(capsys)
    snap = pr(2, created_at=FUTURE)
    fake.add(snap)
    poll(capsys)
    drop_row(fake, store, capsys, snap)
    fake.add(snap)
    poll(capsys)
    poll(capsys)
    assert events(store) == ["opened"]


def test_a_closed_member_dropped_and_reopened_as_a_new_row_gets_no_second_opened(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_query(capsys)
    poll(capsys)
    snap = pr(2, created_at=FUTURE)
    fake.add(snap)
    poll(capsys)
    fake.update(snap["pr"], state="closed", closed_at=LATER, updated_at=LATER)
    poll(capsys, "--pr", snap["pr"])
    for _ in range(4):  # a closed PR is not in the open listing: the source goes, then the row
        poll(capsys)
    store.conn.execute("DELETE FROM prs WHERE pr_id = ?", (snap["pr_id"],))
    store.conn.commit()
    fake.update(snap["pr"], state="open", closed_at=None, updated_at=LATEST)
    poll(capsys)
    poll(capsys)
    assert events(store)[:2] == ["opened", "closed"] and events(store).count("opened") == 1
