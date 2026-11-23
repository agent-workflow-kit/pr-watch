"""The `mergeability` rule (#38): readiness changes that `ci` and `ready` do not already say."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from pr_watch.diff import diff
from pr_watch.digest import build_digest
from tests.forgectl_support import new_pr
from tests.harness import schema_errors
from tests.test_diff import run, types


def codes(*names: str) -> list[dict[str, Any]]:
    return [{"code": n} for n in names]


def snap(mergeable: Any = True, *blocked: str, **changes: Any) -> dict[str, Any]:
    return new_pr(mergeable=mergeable, blocked_by=codes(*blocked), **changes)


def test_a_conflict_appearing_fires_one_mergeability_with_both_sides() -> None:
    (event,) = run(snap(True), snap(False, "conflicts"))
    assert event.type == "mergeability"
    assert event.payload["from_blocked_by"] == [] and event.payload["blocked_by"] == codes(
        "conflicts"
    )
    assert event.payload["from_mergeable"] is True and event.payload["mergeable"] is False
    assert event.payload["occurred_at"] is None


def test_the_conflict_going_away_fires_again() -> None:
    (event,) = run(snap(False, "conflicts"), snap(True))
    assert event.payload["from_blocked_by"] == codes("conflicts")
    assert event.payload["blocked_by"] == [] and event.payload["mergeable"] is True


@pytest.mark.parametrize("code", ["reviews_required", "changes_requested", "branch_behind"])
def test_the_non_ci_blockers_fire(code: str) -> None:
    (event,) = run(snap(True), snap(True, code))
    assert event.payload["blocked_by"] == codes(code)
    assert event.payload["from_mergeable"] is True and event.payload["mergeable"] is True


def test_discussions_unresolved_and_other_blockers_fire_with_detail() -> None:
    old = snap(True)
    new = new_pr(
        mergeable=True,
        blocked_by=[{"code": "discussions_unresolved"}, {"code": "other", "detail": "locked"}],
    )
    (event,) = run(old, new)
    assert event.payload["blocked_by"] == [
        {"code": "discussions_unresolved"},
        {"code": "other", "detail": "locked"},
    ]


def test_a_second_blocker_joining_fires_with_the_whole_set() -> None:
    (event,) = run(snap(False, "conflicts"), snap(False, "conflicts", "reviews_required"))
    assert event.payload["from_blocked_by"] == codes("conflicts")
    assert event.payload["blocked_by"] == codes("conflicts", "reviews_required")


def test_the_same_readiness_is_silent() -> None:
    assert run(snap(False, "conflicts"), snap(False, "conflicts")) == []
    assert run(snap(True), snap(True)) == []


def test_ci_blockers_never_fire_it() -> None:
    assert run(snap(True), snap(True, "checks_failing")) == []
    assert run(snap(True, "checks_pending"), snap(True, "checks_failing")) == []
    assert run(snap(True, "checks_failing"), snap(True)) == []


def test_ci_blockers_alongside_a_readiness_blocker_are_left_out_of_the_payload() -> None:
    (event,) = run(snap(True, "checks_pending"), snap(False, "checks_failing", "conflicts"))
    assert event.payload["from_blocked_by"] == []
    assert event.payload["blocked_by"] == codes("conflicts")


def test_the_draft_blocker_never_fires_it_and_never_shows_in_the_payload() -> None:
    got = run(snap(True), snap(True, "draft", draft=True))
    assert types(got) == ["converted_to_draft"]
    got = run(snap(True, "draft", draft=True), snap(True, draft=False))
    assert types(got) == ["ready"]
    (event,) = run(snap(True, "draft", draft=True), snap(False, "draft", "conflicts", draft=True))
    assert event.payload["from_blocked_by"] == [] and event.payload["blocked_by"] == codes(
        "conflicts"
    )


def test_a_mergeable_flip_caused_by_draft_is_left_to_the_draft_events() -> None:
    # GitLab reads a draft as not mergeable with no readiness code of its own
    got = run(snap(True), snap(False, "draft", draft=True))
    assert types(got) == ["converted_to_draft"]
    got = run(snap(False, "draft", draft=True), snap(True, draft=False))
    assert types(got) == ["ready"]


def test_a_mergeable_flip_that_comes_with_a_ci_change_is_left_to_ci() -> None:
    assert run(snap(True), snap(False, "checks_failing")) == []
    assert run(snap(False, "checks_pending"), snap(True)) == []


def test_a_mergeable_flip_with_nothing_else_changing_fires() -> None:
    (event,) = run(snap(True), snap(False))
    assert event.payload["from_mergeable"] is True and event.payload["mergeable"] is False
    assert event.payload["from_blocked_by"] == event.payload["blocked_by"] == []


# --- unknown and computing -----------------------------------------------------------------


def test_an_unknown_reading_is_no_event_either_way() -> None:
    unknown = snap(None, "mergeability_unknown")
    assert run(snap(False, "conflicts"), unknown) == []
    assert run(unknown, snap(True)) == []
    assert run(unknown, unknown) == []


def test_the_last_known_reading_is_held_across_unknown_so_the_change_still_fires() -> None:
    a, unknown, b = snap(False, "conflicts"), snap(None, "mergeability_unknown"), snap(True)
    d0 = build_digest(a)
    d1 = build_digest(unknown, d0)
    assert diff(d0, d1, unknown) == []
    assert d1["blocked_by"] == codes("mergeability_unknown")  # the unsettled machinery's input
    assert d1["readiness"] == d0["readiness"]
    d2 = build_digest(b, d1)
    (event,) = diff(d1, d2, b)
    assert (
        event.payload["from_blocked_by"] == codes("conflicts") and event.payload["blocked_by"] == []
    )
    assert event.payload["from_mergeable"] is False and event.payload["mergeable"] is True


def test_flapping_through_unknown_back_to_the_same_reading_is_silent() -> None:
    a, unknown = snap(False, "conflicts"), snap(None, "mergeability_unknown")
    d0 = build_digest(a)
    d1 = build_digest(unknown, d0)
    d2 = build_digest(a, d1)
    assert diff(d1, d2, a) == []


def test_unknown_blocker_among_others_is_still_unknown() -> None:
    a = snap(False, "conflicts")
    mixed = snap(None, "mergeability_unknown", "reviews_required")
    assert run(a, mixed) == []


def test_a_null_mergeable_without_the_unknown_code_keeps_the_last_bool() -> None:
    d0 = build_digest(snap(True))
    s = snap(None)
    d1 = build_digest(s, d0)
    assert d1["readiness"]["mergeable"] is True and diff(d0, d1, s) == []
    s2 = snap(False)
    d2 = build_digest(s2, d1)
    (event,) = diff(d1, d2, s2)
    assert event.payload["from_mergeable"] is True and event.payload["mergeable"] is False


def test_the_first_known_reading_is_the_baseline() -> None:
    unknown = snap(None, "mergeability_unknown")
    assert run(unknown, snap(False, "conflicts")) == []
    assert build_digest(unknown)["readiness"] == {"blocked_by": None, "mergeable": None}


def test_an_unreadable_blocked_by_keeps_the_last_reading() -> None:
    d0 = build_digest(snap(False, "conflicts"))
    bad = snap(False)
    bad["blocked_by"] = "oops"
    d1 = build_digest(bad, d0)
    assert d1["readiness"]["blocked_by"] == codes("conflicts")
    assert diff(d0, d1, bad) == []


# --- silent cases ------------------------------------------------------------------------


def test_first_sight_is_silent() -> None:
    assert run(None, snap(False, "conflicts")) == []


def test_rebaseline_is_silent() -> None:
    assert run(snap(True), snap(False, "conflicts"), rebaseline=True) == []


def test_a_stored_digest_without_readiness_fills_silently() -> None:
    old = build_digest(snap(True))
    del old["readiness"]
    new_snap = snap(False, "conflicts")
    new = build_digest(new_snap, old)
    assert new["readiness"]["blocked_by"] == codes("conflicts")
    assert diff(old, new, new_snap) == []


@pytest.mark.parametrize("bad", [None, 7, "x", [], {"blocked_by": 3, "mergeable": True}, {}])
def test_a_malformed_stored_readiness_fills_silently(bad: Any) -> None:
    old = build_digest(snap(True))
    old["readiness"] = bad
    new_snap = snap(False, "conflicts")
    new = build_digest(new_snap, old)
    assert diff(old, new, new_snap) == []


def test_closing_or_merging_is_not_a_readiness_change() -> None:
    # forgectl reports `blocked_by: []` and `mergeable: null` for a closed or merged PR
    done = snap(None, state="closed")
    assert types(run(snap(False, "conflicts"), done)) == ["closed"]
    merged = snap(None, state="merged")
    assert types(run(snap(False, "conflicts"), merged)) == ["merged"]


def test_the_readiness_is_kept_while_the_pr_is_closed() -> None:
    d0 = build_digest(snap(False, "conflicts"))
    d1 = build_digest(snap(None, state="closed"), d0)
    assert d1["readiness"] == d0["readiness"]


def test_a_reopen_is_silent_for_mergeability() -> None:
    got = run(snap(False, "conflicts", state="closed"), snap(True, state="open"))
    assert types(got) == ["reopened"]


def test_merged_is_final() -> None:
    assert run(snap(True, state="merged"), snap(False, "conflicts", state="merged")) == []


def test_it_comes_after_ci_and_ready_and_before_the_terminal_event() -> None:
    old = snap(True, draft=True)
    new = snap(False, "conflicts", draft=False)
    new["ci"] = {**new["ci"], "state": "failing"}
    got = run(old, new)
    assert types(got) == ["ci", "ready", "mergeability"]


def test_blocked_by_is_bounded_and_one_line() -> None:
    long = "x" * 400
    old = snap(True)
    new = new_pr(mergeable=False, blocked_by=[{"code": "other", "detail": long + "\n"}])
    (event,) = run(old, new)
    (item,) = event.payload["blocked_by"]
    assert item["code"] == "other" and len(item["detail"]) <= 256 and "\n" not in item["detail"]


def test_the_event_validates_against_the_schema() -> None:
    (event,) = run(snap(True), snap(False, "conflicts"))
    stored = {
        "id": "ev-aaaaaaaaaaaa", "seq": 1, "type": "mergeability", "pr": event.pr,
        "pr_id": event.pr_id, "detected_at": "2026-10-03T10:00:00.000000Z", "labels": [],
        "watches": [], **copy.deepcopy(dict(event.payload)),
    }  # fmt: skip
    assert schema_errors("event", stored) == []
    assert schema_errors("event", {**stored, "mergeable": "yes"}) != []
    assert schema_errors("event", {**stored, "blocked_by": [{"code": "Bad Code"}]}) != []
    stored["mergeable"] = None
    stored["from_mergeable"] = None
    assert schema_errors("event", stored) == []
