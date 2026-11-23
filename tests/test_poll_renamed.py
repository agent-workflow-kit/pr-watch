"""`renamed` through `poll` against the fake forgectl (#37): exactly one event per rename, whether
a listing or a snapshot sees it first."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pr_watch.state_dir import open_state_dir
from pr_watch.store import Store, open_store
from pr_watch.watches import new_watch_id
from tests.forgectl_support import FakeForgectl, sha
from tests.harness import schema_errors
from tests.test_poll import (
    HOST,
    LATER,
    LATEST,
    col,
    events,
    log_records,
    poll,
    pr,
    run,
    watch_pr,
    watch_query,
)

NEW_REPO = f"{HOST}/example-org/gadgets"
T3, T4 = "2026-10-05T00:00:00Z", "2026-10-06T00:00:00Z"


@pytest.fixture
def store(state_dir: Path) -> Iterator[Store]:
    with open_state_dir(state_dir) as state, open_store(state) as s:
        yield s


@pytest.fixture
def fake(fake_forgectl: FakeForgectl) -> FakeForgectl:
    return fake_forgectl.host(HOST, me="user-1")


def stored(store: Store) -> list[dict[str, Any]]:
    return [
        {"type": r[0], "pr": r[1], "payload": json.loads(r[2])}
        for r in store.conn.execute("SELECT type, pr, payload FROM events ORDER BY seq")
    ]


def test_a_rename_seen_by_a_listing_is_one_event_and_the_snapshot_adds_none(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    old = pr(1)
    fake.add(old)
    watch_query(capsys)
    poll(capsys)
    new = fake.rename(old["pr"], NEW_REPO)
    _, doc = poll(capsys)  # the query listing shows the new name; nothing else changed
    assert doc["events"]["renamed"] == 1 and doc["prs"]["snapshotted"] == 0
    assert events(store) == ["renamed"]
    (event,) = stored(store)
    assert event["pr"] == new and event["payload"]["repo"] == NEW_REPO
    assert event["payload"]["from_pr"] == old["pr"] and event["payload"]["to_pr"] == new
    assert event["payload"]["from_repo"] == old["repo"] and event["payload"]["to_repo"] == NEW_REPO
    assert event["payload"]["head_sha"] == old["head"]["sha"]
    assert event["payload"]["occurred_at"] is None
    assert col(store, old["pr_id"], "pr") == new and col(store, old["pr_id"], "repo") == NEW_REPO
    # the snapshot that follows finds the name already stored: no second event
    fake.update(new, updated_at=LATER)
    _, doc = poll(capsys)
    assert doc["prs"]["snapshotted"] == 1 and sum(doc["events"].values()) == 0
    assert events(store) == ["renamed"]
    poll(capsys)
    assert events(store) == ["renamed"]


def test_a_rename_seen_first_by_a_snapshot_is_one_event_and_a_later_listing_adds_none(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    old = pr(2)
    fake.add(old)
    watch_pr(capsys, old["pr"])  # a per-PR watch: its repo listing names the old path
    new = fake.rename(old["pr"], NEW_REPO)
    fake.update(new, updated_at=LATER)
    _, doc = poll(capsys, "--pr", old["pr"])
    assert doc["events"]["renamed"] == 1 and events(store) == ["renamed"]
    assert stored(store)[0]["pr"] == new
    assert col(store, old["pr_id"], "pr") == new
    watch_query(capsys)
    fake.update(new, updated_at=LATEST)
    poll(capsys)
    poll(capsys, "--full")
    assert events(store) == ["renamed"]


def test_rename_and_push_in_one_snapshot_come_in_the_documented_order(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    old = pr(3, base={"ref": "main"})
    fake.add(old)
    watch_pr(capsys, old["pr"])
    new = fake.rename(old["pr"], NEW_REPO)
    fake.update(new, updated_at=LATER, head={"sha": sha(9)}, base={"ref": "dev"})
    poll(capsys, "--pr", old["pr"])
    assert events(store) == ["renamed", "pushed", "retargeted"]
    assert {e["pr"] for e in stored(store)} == {new}
    assert [e["payload"]["head_sha"] for e in stored(store)] == [sha(9)] * 3


def test_a_case_only_rename_refreshes_the_name_and_fires_nothing(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    old = pr(4)
    fake.add(old)
    watch_query(capsys)
    poll(capsys)
    new = fake.rename(old["pr"], f"{HOST}/Example-Org/Widgets")
    _, doc = poll(capsys)
    assert sum(doc["events"].values()) == 0 and events(store) == []
    assert col(store, old["pr_id"], "pr") == new


def test_a_row_without_a_digest_is_renamed_silently(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    old = pr(5)
    fake.add(old)
    code, out, _ = run(
        capsys, "--json", "watch", "add", old["pr"], "--owner", "human:a", "--no-baseline"
    )
    assert code == 0, out
    new = fake.rename(old["pr"], NEW_REPO)
    fake.update(new, updated_at=LATER)
    poll(capsys, "--pr", old["pr"])
    assert events(store) == [] and col(store, old["pr_id"], "pr") == new  # baselined silently


def test_a_merged_pr_is_final_and_gets_no_renamed(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    old = pr(6, state="merged", merged_at="2026-10-02T10:00:00Z", merge_commit_sha=sha(4),
             merged_head_sha=sha(3))  # fmt: skip
    fake.add(old)
    watch_pr(capsys, old["pr"])
    assert events(store) == ["merged"]
    new = fake.rename(old["pr"], NEW_REPO)
    fake.update(new, updated_at=LATER)
    poll(capsys, "--pr", old["pr"])
    assert events(store) == ["merged"]


def test_the_event_validates_and_filters_by_type(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    old = pr(7)
    fake.add(old)
    watch_query(capsys)
    poll(capsys)
    fake.rename(old["pr"], NEW_REPO)
    poll(capsys)
    code, out, _ = run(
        capsys, "--json", "events", "--since", f"v1.{store.store_id}.0", "--type", "renamed"
    )
    assert code == 0
    (event,) = json.loads(out)["events"]
    assert schema_errors("event", event) == [] and len(event["watches"]) == 1


def test_two_renames_are_two_events(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    old = pr(8)
    fake.add(old)
    watch_query(capsys)
    poll(capsys)
    mid = fake.rename(old["pr"], NEW_REPO)
    poll(capsys)
    back = fake.rename(mid, f"{HOST}/example-org/widgets")
    poll(capsys)
    assert events(store) == ["renamed", "renamed"]
    assert back == old["pr"]
    got = [(e["payload"]["from_pr"], e["payload"]["to_pr"]) for e in stored(store)]
    assert got == [(old["pr"], mid), (mid, old["pr"])]


def test_a_listed_rename_with_an_over_cap_label_union_is_stored_and_logged(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str], state_dir: Path
) -> None:
    old = pr(9)
    fake.add(old)
    watch_query(capsys)
    poll(capsys)
    with store.transaction() as conn:
        for n in range(5):
            wid = new_watch_id()
            conn.execute(
                "INSERT INTO watches VALUES (?, 'pr', ?, '2026-01-01T00:00:00.000000Z')",
                (wid, f"k{n}"),
            )
            conn.execute("INSERT INTO owners VALUES (?, 'o')", (wid,))
            conn.execute("INSERT INTO sources (pr_id, watch_id) VALUES (?, ?)", (old["pr_id"], wid))
            for i in range(60):
                conn.execute("INSERT INTO labels VALUES (?, 'o', ?)", (wid, f"team=l{n}-{i:03d}"))
    fake.rename(old["pr"], NEW_REPO)
    _, doc = poll(capsys)  # the listing stores the rename; it must not wedge on the label union
    assert doc["events"]["renamed"] == 1 and events(store) == ["renamed"]
    labels = json.loads(store.conn.execute("SELECT labels FROM events").fetchone()[0])
    assert len(labels) == 256
    (rec,) = [r for r in log_records(state_dir) if r["event"] == "labels_capped"]
    assert (rec["pr_id"], rec["kept"], rec["total"]) == (old["pr_id"], 256, 300)
    _, doc = poll(capsys)  # and the next poll is not stuck on it
    assert events(store) == ["renamed"]


def test_poll_end_counts_the_renamed_events_a_listing_stored(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str], state_dir: Path
) -> None:
    old = pr(10)
    fake.add(old)
    watch_query(capsys)
    poll(capsys)
    fake.rename(old["pr"], NEW_REPO)
    poll(capsys)  # no snapshot: the listing's one event is the whole poll's
    ends = [r for r in log_records(state_dir) if r["event"] == "poll_end"]
    assert [r["events"] for r in ends] == [0, 1]


def test_a_stored_digest_without_a_readable_state_is_treated_as_never_snapshotted(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str], state_dir: Path
) -> None:
    old = pr(11)
    fake.add(old)
    watch_query(capsys)
    poll(capsys)
    with store.transaction() as conn:
        conn.execute('UPDATE prs SET digest = \'{"head_sha": null, "state": null}\'')
    new = fake.rename(old["pr"], NEW_REPO)
    poll(capsys)
    assert events(store) == [] and col(store, old["pr_id"], "pr") == new
    assert [r for r in log_records(state_dir) if r["event"] == "events_dropped"] == []
