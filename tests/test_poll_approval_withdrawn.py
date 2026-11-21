"""`approval_withdrawn` through `poll` against the fake forgectl (#36)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pr_watch.state_dir import open_state_dir
from pr_watch.store import Store, open_store
from tests.forgectl_support import FakeForgectl, sha
from tests.harness import schema_errors
from tests.test_poll import GL, HOST, LATER, LATEST, events, poll, pr, run, watch_pr


@pytest.fixture
def store(state_dir: Path) -> Iterator[Store]:
    with open_state_dir(state_dir) as state, open_store(state) as s:
        yield s


@pytest.fixture
def fake(fake_forgectl: FakeForgectl) -> FakeForgectl:
    return fake_forgectl.host(HOST, me="user-1").host(GL, backend="gitlab", me="user-1")


def verdict(state: str, at: str, ident: str = "77", commit: Any = None) -> dict[str, Any]:
    return {
        "at": at,
        "can_push": True,
        "commit_sha": commit if commit is not None else sha(1),
        "id": ident,
        "login": "reviewer-1",
        "state": state,
    }


def test_github_approve_dismiss_reapprove_withdraw_sequence(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(1, latest_by_reviewer=[])
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    fake.update(
        snap["pr"],
        updated_at=LATER,
        latest_by_reviewer=[verdict("approved", "2026-10-03T01:00:00Z")],
    )
    poll(capsys)
    fake.update(
        snap["pr"],
        updated_at=LATEST,
        latest_by_reviewer=[verdict("dismissed", "2026-10-03T02:00:00Z")],
    )
    _, doc = poll(capsys)
    assert doc["events"]["approval_withdrawn"] == 1
    assert events(store) == ["approved", "approval_withdrawn"]
    fake.update(
        snap["pr"],
        updated_at="2026-10-05T00:00:00Z",
        latest_by_reviewer=[verdict("approved", "2026-10-03T03:00:00Z")],
    )
    poll(capsys)
    fake.update(snap["pr"], updated_at="2026-10-06T00:00:00Z", latest_by_reviewer=[])
    poll(capsys)
    assert events(store) == ["approved", "approval_withdrawn", "approved", "approval_withdrawn"]
    code, out, _ = run(
        capsys,
        "--json",
        "events",
        "--since",
        f"v1.{store.store_id}.0",
        "--type",
        "approval_withdrawn",
    )
    assert code == 0
    got = json.loads(out)["events"]
    assert [e["to"] for e in got] == ["dismissed", None]
    for event in got:
        assert schema_errors("event", event) == []


def test_gitlab_unapprove_keeps_approvals_at_head_null(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    approved = verdict("approved", "2026-10-03T01:00:00Z", "12") | {"commit_sha": None}
    snap = pr(2, host=GL, latest_by_reviewer=[approved])
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    fake.update(snap["pr"], updated_at=LATER, latest_by_reviewer=[])
    poll(capsys)
    assert events(store) == ["approval_withdrawn"]
    row = store.conn.execute("SELECT payload FROM events").fetchone()
    assert json.loads(row[0])["approvals_at_head"] is None
