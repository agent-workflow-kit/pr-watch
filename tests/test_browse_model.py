"""`browse` view model: reads, sanitizing, filters. No Textual needed."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pr_watch import browse_model as bm
from pr_watch.config import load_config
from pr_watch.state_dir import open_state_dir
from pr_watch.store import NewEvent, Store, open_store
from tests.test_list import (
    T0,
    add_pr,
    add_watch,
    digest,
    fingerprint,
    link,
    populate,
    set_meta,
    settled,
)

NOW = datetime(2026, 6, 2, 0, 0, tzinfo=UTC)
BASE = {"repo": "github.com/o/r", "occurred_at": None, "head_sha": "a" * 40, "watches": []}


@pytest.fixture
def store(state_dir: Path) -> Iterator[Store]:
    with open_state_dir(state_dir) as state, open_store(state) as s:
        yield s


def add_event(
    store: Store, type_: str = "closed", pr: str = "github.com/o/r#1", **kw: object
) -> int:
    labels = list(kw.pop("labels", ()))  # type: ignore[call-overload]
    with store.transaction():
        return store.append_event(
            NewEvent(type_, "P1", pr, {**BASE, **kw}, labels), datetime(2026, 6, 1, tzinfo=UTC)
        ).seq


def load() -> bm.Snapshot:
    return bm.load_snapshot(load_config(None), now=NOW)


def test_missing_state_dir_is_an_empty_snapshot_and_creates_nothing(state_dir: Path) -> None:
    snap = load()
    assert snap.empty
    assert (snap.prs, snap.watches, snap.events) == ((), (), ())
    assert not state_dir.exists()


def test_populated_snapshot_has_items_in_order(store: Store) -> None:
    populate(store)
    add_event(store, "closed")
    add_event(store, "pushed", from_sha="b" * 40, to_sha="a" * 40, forced=None)
    snap = load()
    assert not snap.empty
    assert [i.key for i in snap.watches] == ["w-aaaaaaaaaa", "w-bbbbbbbbbb"]
    assert [i.key for i in snap.prs] == ["P1", "P2", "P3", "P4", "P6"]
    assert {i.status for i in snap.prs} == {"active", "terminal", "unreachable"}
    assert [i.cells[2] for i in snap.events] == ["pushed", "closed"]  # newest first
    assert snap.last_poll == "2026-06-01T02:00:00.000000Z"
    assert any(h.split()[0] == "github.com" for h in snap.hosts)


def test_pr_labels_are_the_union_of_its_watches(store: Store) -> None:
    populate(store)
    by_key = {i.key: i for i in load().prs}
    assert by_key["P1"].labels == ("team=x", "team=y", "tier=1")
    assert by_key["P2"].labels == ("team=x",)


def test_events_are_a_recent_window(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bm, "EVENT_WINDOW", 3)
    for _ in range(5):
        add_event(store)
    assert [i.key for i in load().events] == ["5", "4", "3"]


def test_hostile_stored_text_is_sanitized_everywhere(store: Store) -> None:
    evil = "x\x1b[31m‮yy\nz"
    add_watch(store, "w-cccccccccc", "query", "mine+open;host=h", {"human:a": [f"k={evil}"]})
    add_pr(store, "P9", f"github.com/o/{evil}#9", dig=digest(), sources=())
    link(store, "P9", "w-cccccccccc")
    with store.transaction() as c:  # the writer validates; a corrupted store may hold anything
        c.execute(
            "INSERT INTO events (id, type, pr_id, pr, payload, detected_at, labels) "
            "VALUES ('e1', ?, 'P9', ?, ?, ?, ?)",
            (f"t{evil}", f"github.com/o/{evil}#9", json.dumps({"note": evil}), T0,
             json.dumps([f"k={evil}"])),
        )  # fmt: skip
    snap = load()
    shown = [c for i in (*snap.prs, *snap.watches, *snap.events) for c in (*i.cells, i.detail)]
    shown += [lbl for i in (*snap.prs, *snap.watches, *snap.events) for lbl in i.labels]
    joined = "\n".join(shown)
    assert "\x1b" not in joined and "‮" not in joined
    assert "<U+001B>" in joined and "<U+202E>" in joined
    for i in (*snap.prs, *snap.watches, *snap.events):
        assert all("\n" not in c for c in i.cells)


def test_filters(store: Store) -> None:
    populate(store)
    prs = load().prs
    assert [i.key for i in bm.visible(prs, status="terminal")] == ["P2"]
    assert [i.key for i in bm.visible(prs, label="tier=1")] == ["P1"]
    assert [i.key for i in bm.visible(prs, text="O/S#3")] == ["P3"]
    assert [i.key for i in bm.visible(prs, status="active", label="team=x")] == ["P1", "P4", "P6"]
    assert len(bm.visible(prs)) == len(prs)
    watches = load().watches  # status does not apply to watches
    assert len(bm.visible(watches, status="terminal")) == len(watches)
    assert bm.label_choices(prs) == ["team=x", "team=y", "tier=1"]


def test_age_text() -> None:
    t = NOW
    assert bm.age_text(t, t) == "0s"
    assert bm.age_text(datetime(2026, 6, 1, 23, 59, 15, tzinfo=UTC), t) == "45s"
    assert bm.age_text(datetime(2026, 6, 1, 23, 50, tzinfo=UTC), t) == "10m"
    assert bm.age_text(datetime(2026, 6, 1, 18, tzinfo=UTC), t) == "6h"
    assert bm.age_text(datetime(2026, 5, 28, tzinfo=UTC), t) == "5d"
    assert bm.age_text(datetime(2026, 6, 3, tzinfo=UTC), t) == "0s"  # clock skew never negative


def test_pr_detail_is_show_text_and_missing_pr_is_none(store: Store) -> None:
    populate(store)
    cfg = load_config(None)
    text = bm.load_pr_detail(cfg, "P1", now=NOW)
    assert text is not None and "github.com/o/r#1" in text and "reviewers (0)" in text
    assert bm.load_pr_detail(cfg, "P404", now=NOW) is None


def test_reading_leaves_the_state_directory_byte_identical(store: Store, state_dir: Path) -> None:
    populate(store)
    add_event(store)
    set_meta(store, "x", T0)
    store.close()
    before = fingerprint(state_dir)
    load()
    bm.load_pr_detail(load_config(None), "P1", now=NOW)
    assert settled(fingerprint(state_dir)) == settled(before)
