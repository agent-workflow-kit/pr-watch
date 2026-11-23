"""`retargeted` through `poll` against the fake forgectl (#37): multi-snapshot sequences."""

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
from tests.test_poll import HOST, LATER, LATEST, events, poll, pr, run, watch_pr

T3, T4, T5 = "2026-10-05T00:00:00Z", "2026-10-06T00:00:00Z", "2026-10-07T00:00:00Z"


@pytest.fixture
def store(state_dir: Path) -> Iterator[Store]:
    with open_state_dir(state_dir) as state, open_store(state) as s:
        yield s


@pytest.fixture
def fake(fake_forgectl: FakeForgectl) -> FakeForgectl:
    return fake_forgectl.host(HOST, me="user-1")


def payloads(store: Store) -> list[dict[str, Any]]:
    return [json.loads(r[0]) for r in store.conn.execute("SELECT payload FROM events ORDER BY seq")]


def test_retarget_once_then_back_fires_twice_and_a_quiet_poll_fires_none(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(1, base={"ref": "main"})
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    assert events(store) == []
    fake.update(snap["pr"], updated_at=LATER, base={"ref": "release/1.x"})
    _, doc = poll(capsys)
    assert doc["events"]["retargeted"] == 1 and events(store) == ["retargeted"]
    got = payloads(store)[0]
    assert (got["from"], got["to"]) == ("main", "release/1.x")
    fake.update(snap["pr"], updated_at=LATEST)  # nothing changed but the time
    poll(capsys)
    assert events(store) == ["retargeted"]
    fake.update(snap["pr"], updated_at=T3, base={"ref": "main"})
    poll(capsys)
    assert events(store) == ["retargeted", "retargeted"]
    assert (payloads(store)[1]["from"], payloads(store)[1]["to"]) == ("release/1.x", "main")


def test_the_event_is_valid_and_comes_with_a_push_in_order(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(2, base={"ref": "main"})
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    fake.update(
        snap["pr"], updated_at=LATER, base={"ref": "dev"}, head={"sha": "c" * 40},
    )  # fmt: skip
    poll(capsys)
    assert events(store) == ["pushed", "retargeted"]
    code, out, _ = run(
        capsys, "--json", "events", "--since", f"v1.{store.store_id}.0", "--type", "retargeted"
    )
    assert code == 0
    (event,) = json.loads(out)["events"]
    assert schema_errors("event", event) == [] and event["head_sha"] == "c" * 40


@pytest.mark.parametrize("bad", [""])
def test_an_unknown_base_is_silent_and_keeps_the_stored_name(
    bad: Any, fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(3, base={"ref": "main"})
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    fake.update(snap["pr"], updated_at=LATER, base={"ref": bad})
    poll(capsys)
    assert events(store) == []
    fake.update(snap["pr"], updated_at=LATEST, base={"ref": "main"})
    poll(capsys)
    assert events(store) == []  # the stored name was kept, so back to main is no change
    fake.update(snap["pr"], updated_at=T3, base={"ref": "dev"})
    poll(capsys)
    assert events(store) == ["retargeted"]


def test_a_stored_digest_without_base_ref_is_filled_silently(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(4, base={"ref": "main"})
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    row = store.conn.execute("SELECT pr_id, digest FROM prs").fetchone()
    digest = json.loads(row[1])
    del digest["base_ref"]  # a digest written before 1.x
    store.conn.execute("UPDATE prs SET digest = ? WHERE pr_id = ?", (json.dumps(digest), row[0]))
    fake.update(snap["pr"], updated_at=LATER, base={"ref": "dev"})
    poll(capsys)
    assert events(store) == []
    assert (
        json.loads(store.conn.execute("SELECT digest FROM prs").fetchone()[0])["base_ref"] == "dev"
    )
    fake.update(snap["pr"], updated_at=LATEST, base={"ref": "main"})
    poll(capsys)
    assert events(store) == ["retargeted"]


def test_a_reopen_with_a_new_base_fires_no_retargeted(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = pr(5, base={"ref": "main"}, state="closed")
    snap["closed_at"] = "2026-10-02T11:00:00Z"
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    fake.update(snap["pr"], updated_at=LATER, state="open", closed_at=None, base={"ref": "dev"})
    poll(capsys)
    assert events(store) == ["closed", "reopened"]  # the baseline of a closed PR, then the reopen
