"""The `retargeted` rule (#37): the base branch name changes between two snapshots."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from pr_watch.diff import diff
from pr_watch.digest import build_digest
from tests.forgectl_support import new_pr
from tests.harness import schema_errors
from tests.test_diff import run, types, with_


def snap(ref: Any = "main", **changes: Any) -> dict[str, Any]:
    return new_pr(base={"ref": ref}, **changes)


def test_a_changed_base_ref_fires_one_retargeted_with_from_and_to() -> None:
    (event,) = run(snap("main"), snap("release/1.x"))
    assert event.type == "retargeted"
    assert event.payload["from"] == "main" and event.payload["to"] == "release/1.x"
    assert event.payload["occurred_at"] is None


def test_the_same_base_ref_is_silent() -> None:
    assert run(snap("main"), snap("main")) == []


def test_retargeting_back_fires_again() -> None:
    a, b = snap("main"), snap("dev")
    assert [e.payload["to"] for e in run(a, b)] == ["dev"]
    assert [e.payload["to"] for e in run(b, a)] == ["main"]


def test_a_case_only_change_is_a_retarget() -> None:
    # git ref names are case-sensitive
    (event,) = run(snap("Main"), snap("main"))
    assert (event.payload["from"], event.payload["to"]) == ("Main", "main")


@pytest.mark.parametrize("bad", [None, "", 7, ["main"], True])
def test_an_unreadable_new_base_ref_is_no_event_and_keeps_the_stored_one(bad: Any) -> None:
    old = build_digest(snap("main"))
    new_snap = snap(bad)
    new = build_digest(new_snap, old)
    assert new["base_ref"] == "main"
    assert diff(old, new, new_snap) == []


def test_an_unknown_stored_base_ref_fills_silently() -> None:
    old = build_digest(snap(None))
    assert old["base_ref"] is None
    assert run(snap(None), snap("dev")) == []


def test_a_stored_digest_without_base_ref_fills_silently() -> None:
    old = build_digest(snap("main"))
    del old["base_ref"]
    new_snap = snap("dev")
    new = build_digest(new_snap, old)
    assert new["base_ref"] == "dev"
    assert diff(old, new, new_snap) == []


def test_first_sight_is_silent() -> None:
    assert run(None, snap("dev")) == []


def test_rebaseline_is_silent() -> None:
    assert run(snap("main"), snap("dev"), rebaseline=True) == []


def test_a_reopen_with_a_new_base_is_silent_but_reopened_fires() -> None:
    got = run(snap("main", state="closed"), snap("dev", state="open"))
    assert types(got) == ["reopened"]


def test_merged_is_final() -> None:
    assert run(snap("main", state="merged"), snap("dev", state="merged")) == []


def test_the_base_ref_is_bounded_and_one_line() -> None:
    long_a, long_b = "a" * 300, "b" * 300
    (event,) = run(snap(long_a), snap(long_b))
    assert len(event.payload["from"]) == 256 and event.payload["from"].endswith("...")
    (event,) = run(snap("a\nb"), snap("c‮d"))
    assert event.payload["from"] == "a<U+000A>b" and "<U+202E>" in event.payload["to"]


def test_retargeted_comes_after_pushed_and_before_review() -> None:
    old = snap("main")
    new_snap = with_(snap("dev"), head={**old["head"], "sha": "b" * 40})
    new_snap = with_(new_snap, draft=not old["draft"])
    got = run(old, new_snap)
    order = types(got)
    assert order.index("pushed") < order.index("retargeted") < order.index(
        "converted_to_draft"
    ) or (order.index("pushed") < order.index("retargeted") < order.index("ready"))


def test_the_event_validates_against_the_schema() -> None:
    old, new_snap = snap("main"), snap("dev")
    (event,) = run(old, new_snap)
    stored = {
        "id": "ev-aaaaaaaaaaaa", "seq": 1, "type": "retargeted", "pr": event.pr,
        "pr_id": event.pr_id, "detected_at": "2026-10-03T10:00:00.000000Z", "labels": [],
        "watches": [], **copy.deepcopy(dict(event.payload)),
    }  # fmt: skip
    assert schema_errors("event", stored) == []
    assert schema_errors("event", {**stored, "to": 7}) != []
