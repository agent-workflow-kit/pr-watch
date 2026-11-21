"""`review_requested` and `converted_to_draft` through `poll` against the fake forgectl (#35)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pr_watch.state_dir import open_state_dir
from pr_watch.store import Store, open_store
from tests.forgectl_support import FakeForgectl
from tests.harness import schema_errors
from tests.test_poll import GL, HOST, LATER, LATEST, events, poll, pr, run, watch_pr


@pytest.fixture
def store(state_dir: Path) -> Iterator[Store]:
    with open_state_dir(state_dir) as state, open_store(state) as s:
        yield s


@pytest.fixture
def fake(fake_forgectl: FakeForgectl) -> FakeForgectl:
    return fake_forgectl.host(HOST, me="user-1").host(GL, backend="gitlab", me="user-1")


def req(login: str, ident: str, kind: str = "user", state: str = "outstanding") -> dict[str, Any]:
    return {
        "code_owner": False,
        "instance": None,
        "requested_at": "2026-10-03T00:00:00Z",
        "reviewer": {"id": ident, "kind": kind, "login": login},
        "state": state,
    }


def payloads(store: Store, type_: str) -> list[dict[str, Any]]:
    rows = store.conn.execute("SELECT payload FROM events WHERE type = ? ORDER BY seq", (type_,))
    return [json.loads(r[0]) for r in rows]


def test_github_request_sequence_fires_once_per_new_request_and_again_after_a_review(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(1, review_requests=[req("alice", "1")])
    fake.add(snap)
    watch_pr(capsys, snap["pr"])  # baseline: silent
    assert events(store) == []
    fake.update(
        snap["pr"], updated_at=LATER, review_requests=[req("alice", "1"), req("core", "7", "team")]
    )
    _, doc = poll(capsys)
    assert doc["events"]["review_requested"] == 1 and events(store) == ["review_requested"]
    (payload,) = payloads(store, "review_requested")
    assert payload["reviewer"] == {"id": "7", "login": "core", "kind": "team"}
    # nothing changes: no repeat
    fake.update(snap["pr"], updated_at=LATEST)
    poll(capsys)
    assert events(store) == ["review_requested"]
    # alice reviews (request cleared), then is requested again
    fake.update(
        snap["pr"], updated_at="2026-10-05T00:00:00Z", review_requests=[req("core", "7", "team")]
    )
    poll(capsys)
    fake.update(
        snap["pr"],
        updated_at="2026-10-06T00:00:00Z",
        review_requests=[req("core", "7", "team"), req("alice", "1")],
    )
    poll(capsys)
    got = payloads(store, "review_requested")
    assert [p["reviewer"]["login"] for p in got] == ["core", "alice"]
    code, out, _ = run(
        capsys,
        "--json",
        "events",
        "--since",
        f"v1.{store.store_id}.0",
        "--type",
        "review_requested",
    )
    assert code == 0
    for event in json.loads(out)["events"]:
        assert schema_errors("event", event) == []


def test_gitlab_reviewed_to_outstanding_is_a_new_request(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(2, host=GL, review_requests=[req("u2", "12", state="reviewed")])
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    fake.update(
        snap["pr"], updated_at=LATER, review_requests=[req("u2", "12", state="outstanding")]
    )
    poll(capsys)
    assert events(store) == ["review_requested"]
    fake.update(snap["pr"], updated_at=LATEST, review_requests=[req("u2", "12", state="reviewed")])
    poll(capsys)
    assert events(store) == ["review_requested"]


def test_draft_flapping_emits_converted_to_draft_and_ready(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(3, draft=False)
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    for i, draft in enumerate([True, False, True], start=1):
        fake.update(snap["pr"], updated_at=f"2026-10-0{3 + i}T00:00:00Z", draft=draft)
        poll(capsys)
    assert events(store) == ["converted_to_draft", "ready", "converted_to_draft"]


def test_a_snapshot_without_review_requests_never_fires_and_keeps_the_part_unbaselined(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(
        4, review_requests=[req("alice", "1")], incomplete=["review_requests"], complete=False
    )
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    fake.update(snap["pr"], updated_at=LATER, review_requests=[req("alice", "1"), req("bob", "2")])
    poll(capsys)
    assert events(store) == []  # still incomplete
    fake.update(snap["pr"], updated_at=LATEST, incomplete=[], complete=True)
    poll(capsys)
    assert events(store) == []  # baselined silently now
    fake.update(
        snap["pr"],
        updated_at="2026-10-05T00:00:00Z",
        review_requests=[req("alice", "1"), req("bob", "2"), req("eve", "3")],
    )
    poll(capsys)
    assert events(store) == ["review_requested"]
