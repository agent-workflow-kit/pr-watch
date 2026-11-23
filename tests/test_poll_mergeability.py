"""`mergeability` through `poll` against the fake forgectl (#38): multi-snapshot sequences."""

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
from tests.test_poll import HOST, LATER, LATEST, col, events, poll, pr, run, watch_pr

T3, T4, T5, T6 = (f"2026-10-0{n}T00:00:00Z" for n in (5, 6, 7, 8))
CONFLICT = {"mergeable": False, "blocked_by": [{"code": "conflicts"}]}
CLEAN = {"mergeable": True, "blocked_by": []}
UNKNOWN = {"mergeable": None, "blocked_by": [{"code": "mergeability_unknown"}]}


@pytest.fixture
def store(state_dir: Path) -> Iterator[Store]:
    with open_state_dir(state_dir) as state, open_store(state) as s:
        yield s


@pytest.fixture
def fake(fake_forgectl: FakeForgectl) -> FakeForgectl:
    return fake_forgectl.host(HOST, me="user-1")


def payloads(store: Store) -> list[dict[str, Any]]:
    return [json.loads(r[0]) for r in store.conn.execute("SELECT payload FROM events ORDER BY seq")]


def watched(fake: FakeForgectl, capsys: pytest.CaptureFixture[str], n: int, **ch: Any) -> dict:
    snap = pr(n, **ch)
    fake.add(snap)
    watch_pr(capsys, snap["pr"])
    return snap


def test_a_conflict_appearing_and_going_away_is_two_events_and_a_quiet_poll_none(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = watched(fake, capsys, 1)
    fake.update(snap["pr"], updated_at=LATER, **CONFLICT)
    _, doc = poll(capsys)
    assert doc["events"]["mergeability"] == 1 and events(store) == ["mergeability"]
    first = payloads(store)[0]
    assert first["from_blocked_by"] == [] and first["blocked_by"] == [{"code": "conflicts"}]
    assert (first["from_mergeable"], first["mergeable"]) == (True, False)
    assert first["head_sha"] == snap["head"]["sha"] and first["occurred_at"] is None
    fake.update(snap["pr"], updated_at=LATEST)
    poll(capsys)
    assert events(store) == ["mergeability"]
    fake.update(snap["pr"], updated_at=T3, **CLEAN)
    poll(capsys)
    assert events(store) == ["mergeability", "mergeability"]
    assert payloads(store)[1]["from_blocked_by"] == [{"code": "conflicts"}]


def test_a_conflict_resolved_across_a_computing_spell_still_fires_once(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = watched(fake, capsys, 2, **CONFLICT)
    fake.update(snap["pr"], updated_at=LATER, **UNKNOWN)
    poll(capsys)
    assert events(store) == []
    fake.update(snap["pr"], updated_at=LATEST, **UNKNOWN)
    poll(capsys)
    assert events(store) == []
    fake.update(snap["pr"], updated_at=T3, **CLEAN)
    poll(capsys)
    assert events(store) == ["mergeability"]
    assert payloads(store)[0]["from_blocked_by"] == [{"code": "conflicts"}]


def test_a_required_review_blocker_appearing_fires(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = watched(fake, capsys, 3)
    fake.update(snap["pr"], updated_at=LATER, blocked_by=[{"code": "reviews_required"}])
    poll(capsys)
    assert events(store) == ["mergeability"]
    assert payloads(store)[0]["blocked_by"] == [{"code": "reviews_required"}]


def test_ci_and_draft_blockers_do_not_fire_it_but_their_own_events_do(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = watched(fake, capsys, 4)
    fake.update(
        snap["pr"], updated_at=LATER, blocked_by=[{"code": "checks_failing"}],
        ci={"state": "failing"},
    )  # fmt: skip
    poll(capsys)
    assert "mergeability" not in events(store)
    fake.update(
        snap["pr"], updated_at=LATEST, draft=True,
        blocked_by=[{"code": "draft"}, {"code": "checks_failing"}],
    )  # fmt: skip
    poll(capsys)
    assert "mergeability" not in events(store) and "converted_to_draft" in events(store)
    fake.update(snap["pr"], updated_at=T3, draft=False, blocked_by=[])
    poll(capsys)
    assert "mergeability" not in events(store) and "ready" in events(store)


def test_a_stored_digest_without_readiness_is_silent_after_an_upgrade(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = watched(fake, capsys, 5)
    digest = json.loads(col(store, snap["pr_id"], "digest"))
    del digest["readiness"]
    store.conn.execute(
        "UPDATE prs SET digest = ? WHERE pr_id = ?",
        (json.dumps(digest, sort_keys=True, separators=(",", ":")), snap["pr_id"]),
    )
    store.conn.commit()
    fake.update(snap["pr"], updated_at=LATER, **CONFLICT)
    poll(capsys)
    assert events(store) == []
    assert json.loads(col(store, snap["pr_id"], "digest"))["readiness"]["blocked_by"] == [
        {"code": "conflicts"}
    ]
    fake.update(snap["pr"], updated_at=LATEST, **CLEAN)
    poll(capsys)
    assert events(store) == ["mergeability"]  # from the second snapshot on it is live


def test_closing_a_conflicted_pr_is_only_closed_and_the_reopen_is_silent(
    fake: FakeForgectl, store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = watched(fake, capsys, 6, **CONFLICT)
    fake.update(
        snap["pr"], updated_at=LATER, state="closed", closed_at=LATER, mergeable=None,
        blocked_by=[],
    )  # fmt: skip
    poll(capsys)
    assert events(store) == ["closed"]
    fake.update(snap["pr"], updated_at=LATEST, state="open", closed_at=None, **CLEAN)
    poll(capsys, "--pr", snap["pr"])
    assert events(store) == ["closed", "reopened"]


def test_the_event_is_valid_listed_by_type_and_runs_its_hook(
    fake: FakeForgectl, store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = watched(fake, capsys, 7)
    out = tmp_path / "out"
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(f"hooks:\n  on-mergeability:\n    - cmd: 'cat >> {out}'\n")
    cfg.chmod(0o600)
    fake.update(snap["pr"], updated_at=LATER, **CONFLICT)
    code, text, _ = run(capsys, "--config", str(cfg), "--json", "poll")
    assert code == 0 and json.loads(text)["hooks"]["run"] == 1
    from tests.harness import assert_valid

    assert_valid("hooks/on-mergeability", json.loads(out.read_text()))
    code, text, _ = run(
        capsys, "--json", "events", "--since", f"v1.{store.store_id}.0", "--type", "mergeability"
    )
    (event,) = json.loads(text)["events"]
    assert schema_errors("event", event) == []
