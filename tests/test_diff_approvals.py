"""The diff for approvals, changes requested, CI (with the hold) and ready (docs/interface.md,
Events). Rules are driven through snapshot sequences: `Chain` keeps the stored digest the way the
poll will, from `diff(...).digest`.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pr_watch.diff import DiffEvent, DiffResult, Drop, diff
from pr_watch.digest import build_digest
from pr_watch.state_dir import StateDir, open_state_dir
from pr_watch.store import Store, open_store
from tests.forgectl_support import sha
from tests.harness import schema_errors
from tests.test_diff import T1, T2, T3, WATCH, gh, gl, review, with_

T0 = "2026-10-03T09:00:00Z"


class Chain:
    """Feeds snapshots through build_digest and diff, storing `result.digest` like the poll."""

    def __init__(self, first: dict[str, Any], settle: int = 1) -> None:
        self.settle = settle
        self.stored: dict[str, Any] = build_digest(first)
        self.last: DiffResult = DiffResult()

    def step(self, snap: dict[str, Any], **kw: Any) -> list[DiffEvent]:
        new = build_digest(snap, self.stored, rebaseline=bool(kw.get("rebaseline")))
        self.last = diff(self.stored, new, snap, settle_polls=self.settle, **kw)
        self.stored = self.last.digest
        return list(self.last)

    def types(self, snap: dict[str, Any], **kw: Any) -> list[str]:
        return [e.type for e in self.step(snap, **kw)]


def entry(
    rid: str = "1002",
    state: str = "approved",
    at: str | None = T1,
    commit: str | None = None,
    can_push: Any = True,
    login: str = "user-2",
) -> dict[str, Any]:
    return {
        "at": at,
        "can_push": can_push,
        "commit_sha": commit or sha(1),
        "id": rid,
        "login": login,
        "state": state,
    }


def reviewers(base: dict[str, Any], *items: dict[str, Any]) -> dict[str, Any]:
    return with_(base, latest_by_reviewer=list(items))


def check(
    name: str, state: str = "passing", counted: bool = True, url: Any = "u"
) -> dict[str, Any]:
    return {
        "counted": counted,
        "finished_at": None,
        "name": name,
        "required": None,
        "state": state,
        "url": url if url != "u" else f"https://ci.example.test/{name}",
    }


def ci_snap(
    base: dict[str, Any], state: str, *checks: dict[str, Any], head: Any = None
) -> dict[str, Any]:
    snap = copy.deepcopy(base)
    snap["ci"]["state"] = state
    snap["ci"]["checks"] = list(checks)
    if head is not None:
        snap["ci"]["head_sha"] = head
    return snap


# --- approved ----------------------------------------------------------------------------------


def approvable(*extra_reviews: dict[str, Any]) -> dict[str, Any]:
    snap = reviewers(gh(), entry("1002", "dismissed", T0))
    snap["reviews"] = [*snap["reviews"], *extra_reviews]
    return snap


def test_approved_fires_with_reviewer_payload() -> None:
    other = review("gh:r:PRR_a", "approved", T1) | {
        "author": {"id": "1002", "login": "u2", "is_bot": False},
        "submitted_at": T3,
    }
    chain = Chain(approvable(other))
    snap = with_(
        reviewers(approvable(other), entry("1002", "approved", T1, sha(3), True)),
        approvals_at_head=1,
    )
    (event,) = chain.step(snap)
    assert event.type == "approved"
    assert event.payload == {
        "repo": snap["repo"],
        "head_sha": snap["head"]["sha"],
        "occurred_at": "2026-10-03T10:00:00.000000Z",
        "reviewer": {"id": "1002", "login": "u2", "is_bot": False},
        "commit_sha": sha(3),
        "at": "2026-10-03T10:00:00.000000Z",
        "can_push": True,
        "approvals_at_head": 1,
    }


def test_same_approval_again_is_no_event() -> None:
    chain = Chain(approvable())
    snap = reviewers(approvable(), entry())
    assert chain.types(snap) == ["approved"]
    assert chain.types(snap) == []


def test_approval_present_at_baseline_is_silent() -> None:
    chain = Chain(reviewers(gh(), entry()))
    assert chain.types(reviewers(gh(), entry())) == []


def test_reapproval_after_push_fires_again_by_at() -> None:
    chain = Chain(approvable())
    assert chain.types(reviewers(approvable(), entry(at=T1))) == ["approved"]
    pushed = with_(gh(head={"sha": sha(2)}), latest_by_reviewer=[entry(at=T2, commit=sha(2))])
    assert chain.types(pushed) == ["pushed", "approved"]


def test_approval_with_null_at_fires_on_commit_change_only() -> None:
    chain = Chain(reviewers(gl(), entry("12", "dismissed", None)))
    assert chain.types(reviewers(gl(), entry("12", at=None, commit=sha(1)))) == ["approved"]
    assert chain.types(reviewers(gl(), entry("12", at=None, commit=sha(1)))) == []
    assert chain.types(reviewers(gl(), entry("12", at=None, commit=sha(2)))) == ["approved"]


def test_gitlab_approval_unknown_commit_and_approvals() -> None:
    chain = Chain(reviewers(gl(), entry("12", "dismissed", None)))
    snap = reviewers(gl(), entry("12", at=None, can_push=None))
    snap["latest_by_reviewer"][0]["commit_sha"] = None
    snap["approvals_at_head"] = None
    (event,) = chain.step(snap)
    assert event.payload["approvals_at_head"] is None
    assert event.payload["commit_sha"] is None and event.payload["can_push"] is None
    assert event.payload["occurred_at"] is None
    assert event.payload["reviewer"] == {"id": "12", "login": "user-2", "is_bot": None}


def test_at_changed_from_null_to_a_time_fires() -> None:
    chain = Chain(reviewers(gl(), entry("12", "approved", None)))
    assert chain.types(reviewers(gl(), entry("12", "approved", T1))) == ["approved"]


def test_approved_after_changes_requested_and_back() -> None:
    chain = Chain(approvable())
    assert chain.types(reviewers(approvable(), entry(state="changes_requested", at=T1))) == [
        "changes_requested"
    ]
    assert chain.types(reviewers(approvable(), entry(state="approved", at=T2))) == ["approved"]
    assert chain.types(reviewers(approvable(), entry(state="changes_requested", at=T3))) == [
        "changes_requested"
    ]


def test_dismissal_is_no_event_and_a_later_approval_fires() -> None:
    chain = Chain(reviewers(gh(), entry(state="approved", at=T0)))
    assert chain.types(reviewers(gh(), entry(state="dismissed", at=T1))) == []
    assert chain.types(reviewers(gh(), entry(state="approved", at=T2))) == ["approved"]


def test_unknown_reviewer_state_is_no_event_as_to_and_counts_as_no_entry_as_from() -> None:
    chain = Chain(reviewers(gh(), entry(state="weird", at=T0)))
    assert chain.stored["reviewers"]["1002"]["state"] == "unknown"
    # unknown -> unknown and anything -> unknown (as `to`) fire nothing
    assert chain.types(reviewers(gh(), entry(state="weird", at=T1))) == []
    # as `from` it counts as no entry: a later known verdict fires
    assert chain.types(reviewers(gh(), entry(state="approved", at=T1))) == ["approved"]
    other = Chain(reviewers(gh(), entry(state="weird", at=T0)))
    assert other.types(reviewers(gh(), entry(state="changes_requested", at=T1))) == [
        "changes_requested"
    ]
    # an unreadable state keeps the stored (approved) entry: no event, then a new `at` fires
    assert chain.types(reviewers(gh(), entry(state="weird", at=T2))) == []
    assert chain.types(reviewers(gh(), entry(state="approved", at=T3))) == ["approved"]


def test_approval_by_a_bot_reports_is_bot_from_the_reviews() -> None:
    bot = review("gh:r:PRR_bot", "approved", T1) | {
        "author": {"id": "77", "login": "helper[bot]", "is_bot": True}
    }
    chain = Chain(approvable(bot))
    snap = reviewers(approvable(bot), entry("77", login="helper[bot]"))
    (event,) = chain.step(snap)
    assert event.payload["reviewer"] == {"id": "77", "login": "helper[bot]", "is_bot": True}


def _only_reviewer(*items: dict[str, Any], reviews: list[dict[str, Any]] | None = None) -> Any:
    chain = Chain(approvable())
    snap = reviewers(approvable(), *items)
    if reviews is not None:
        snap = with_(snap, reviews=reviews)
    (event,) = (e for e in chain.step(snap) if e.type == "approved")
    return event.payload["reviewer"]


def test_id_keyed_reviewer_without_a_review_takes_the_login_from_the_entry() -> None:
    who = _only_reviewer(entry("55", login="SomeBody"), reviews=[])
    assert who == {"id": "55", "login": "SomeBody", "is_bot": None}


def test_login_keyed_reviewer_has_a_null_id_and_the_original_case_login() -> None:
    nameless = entry(login="Some-User")
    del nameless["id"]
    assert _only_reviewer(nameless) == {"id": None, "login": "Some-User", "is_bot": None}


@pytest.mark.parametrize("flip", [False, True])
def test_login_keyed_duplicates_differing_only_in_case_pick_the_login_independent_of_order(
    flip: bool,
) -> None:
    upper, lower = entry(login="Bob"), entry(login="bob")
    del upper["id"], lower["id"]
    who = _only_reviewer(*([lower, upper] if flip else [upper, lower]))
    assert who == {"id": None, "login": "bob", "is_bot": None}


@pytest.mark.parametrize("flip", [False, True])
def test_id_keyed_duplicates_with_different_logins_pick_the_login_independent_of_order(
    flip: bool,
) -> None:
    first, second = entry("55", login="alpha"), entry("55", login="beta")
    who = _only_reviewer(*([second, first] if flip else [first, second]), reviews=[])
    assert who == {"id": "55", "login": "beta", "is_bot": None}


def test_hashed_overlong_login_key_has_null_id_and_login() -> None:
    nameless = entry(login="A" * 300)
    del nameless["id"]
    assert _only_reviewer(nameless) == {"id": None, "login": None, "is_bot": None}


def test_valid_id_that_looks_like_a_login_key_keeps_its_id() -> None:
    who = _only_reviewer(entry("login:x", login="Foo"), reviews=[])
    assert who == {"id": "login:x", "login": "Foo", "is_bot": None}


def test_review_lookup_by_id_wins_over_the_entry_login() -> None:
    mine = review("gh:r:PRR_m", "approved", T1) | {
        "author": {"id": "1002", "login": "FromReview", "is_bot": False}
    }
    who = _only_reviewer(entry("1002", login="from-entry"), reviews=[mine])
    assert who == {"id": "1002", "login": "FromReview", "is_bot": False}


@pytest.mark.parametrize("flip", [False, True])
def test_duplicate_entries_of_one_key_pick_the_winner_whatever_the_order(flip: bool) -> None:
    old, new = entry(at=T1, login="Some-User"), entry(at=T2, login="SOME-user")
    for item in (old, new):
        del item["id"]
    items = [new, old] if flip else [old, new]
    assert _only_reviewer(*items)["login"] == "SOME-user"


def test_hostile_reviewer_login_is_bounded_and_one_line() -> None:
    nasty = review("gh:r:PRR_h", "approved", T1) | {
        "author": {"id": "1002", "login": "a\nb" + "x" * 1000, "is_bot": "yes"},
        "submitted_at": T3,
    }
    chain = Chain(approvable(nasty))
    (event,) = chain.step(reviewers(approvable(nasty), entry()))
    login = event.payload["reviewer"]["login"]
    assert "\n" not in login and len(login) <= 256
    assert event.payload["reviewer"]["is_bot"] is None


def test_two_reviewers_ordered_by_key() -> None:
    chain = Chain(approvable())
    snap = reviewers(approvable(), entry("2000", at=T1), entry("1500", at=T2), entry("1002", at=T3))
    events = chain.step(snap)
    assert [e.payload["reviewer"]["id"] for e in events] == ["1002", "1500", "2000"]
    assert [e.type for e in events] == ["approved"] * 3


def test_approved_silent_when_reviews_incomplete_unbaselined_or_rebaseline() -> None:
    chain = Chain(approvable())
    snap = reviewers(approvable(), entry())
    snap["incomplete"] = ["reviews"]
    assert chain.types(snap) == []
    assert chain.stored["reviewers"]["1002"]["state"] == "dismissed"
    assert chain.types(reviewers(approvable(), entry()), rebaseline=True) == []
    assert chain.types(reviewers(approvable(), entry(at=T2))) == ["approved"]
    unbaselined = approvable()
    unbaselined["incomplete"] = ["reviews"]
    late = Chain(unbaselined)
    assert late.stored["baselined"]["reviews"] is False
    assert late.types(reviewers(approvable(), entry())) == []
    assert late.types(reviewers(approvable(), entry(at=T2))) == ["approved"]


def test_approval_before_a_push_orders_after_pushed_and_review() -> None:
    chain = Chain(approvable())
    snap = with_(
        reviewers(gh(head={"sha": sha(2)}), entry(at=T1)),
        reviews=[review("gh:r:PRR_x", "approved", T1)],
    )
    assert chain.types(snap) == ["pushed", "review", "approved"]


# --- changes_requested -------------------------------------------------------------------------


def test_changes_requested_fires_with_payload_and_no_approvals_field() -> None:
    chain = Chain(approvable())
    snap = reviewers(approvable(), entry(state="changes_requested", at=T2, commit=sha(3)))
    (event,) = chain.step(snap)
    assert event.type == "changes_requested"
    assert event.payload["at"] == event.payload["occurred_at"] == "2026-10-03T11:00:00.000000Z"
    assert event.payload["commit_sha"] == sha(3)
    assert event.payload["can_push"] is True
    assert "approvals_at_head" not in event.payload


def test_changes_requested_repeats_after_a_push_by_at() -> None:
    chain = Chain(approvable())
    assert chain.types(reviewers(approvable(), entry(state="changes_requested", at=T1))) == [
        "changes_requested"
    ]
    assert chain.types(reviewers(approvable(), entry(state="changes_requested", at=T1))) == []
    assert chain.types(reviewers(approvable(), entry(state="changes_requested", at=T2))) == [
        "changes_requested"
    ]


def test_gitlab_changes_requested_fires_only_on_the_transition() -> None:
    chain = Chain(reviewers(gl(), entry("12", "approved", None)))
    cr = entry("12", "changes_requested", None)
    cr["commit_sha"] = None
    assert chain.types(reviewers(gl(), cr)) == ["changes_requested"]
    again = copy.deepcopy(cr)
    assert chain.types(reviewers(gl(head={"sha": sha(2)}), again)) == ["pushed"]


# --- ci and the hold ---------------------------------------------------------------------------


def settled(state: str = "passing", *checks: dict[str, Any]) -> dict[str, Any]:
    return ci_snap(gh(), state, *(checks or (check("build", state),)))


def test_pending_to_passing_fires_unheld_with_counts_and_checks() -> None:
    chain = Chain(settled("pending", check("build", "pending"), check("lint", "pending")))
    snap = settled("passing", check("build", "passing"), check("lint", "passing"))
    (event,) = chain.step(snap)
    assert event.type == "ci"
    assert event.payload["from"] == "pending" and event.payload["to"] == "passing"
    assert event.payload["counts"] == {"passing": 2, "failing": 0, "pending": 0}
    assert event.payload["for_head"] == snap["ci"]["head_sha"]
    assert event.payload["occurred_at"] is None
    assert event.payload["checks"] == [
        {"name": "build", "from": "pending", "to": "passing"},
        {"name": "lint", "from": "pending", "to": "passing"},
    ]
    assert "checks_more" not in event.payload
    assert chain.stored["ci"]["state"] == "passing" and chain.stored["ci"]["hold"] == 0


def test_pending_failing_passing_transitions_are_never_held() -> None:
    chain = Chain(settled("pending", check("a", "pending")), settle=5)
    assert chain.types(settled("failing", check("a", "failing"))) == ["ci"]
    assert chain.types(settled("pending", check("a", "pending"))) == ["ci"]
    assert chain.types(settled("passing", check("a", "passing"))) == ["ci"]


def test_pending_storm_fires_once() -> None:
    chain = Chain(settled("passing", check("a")))
    snaps = [settled("pending", check("a", "pending"))] * 3
    assert [chain.types(s) for s in snaps] == [["ci"], [], []]


def test_change_involving_none_is_held_until_the_second_snapshot() -> None:
    chain = Chain(settled("passing", check("a")))
    none = ci_snap(gh(), "none")
    assert chain.step(none) == []
    ci = chain.stored["ci"]
    assert (ci["state"], ci["hold"], ci["hold_to"]) == ("passing", 1, "none")
    assert ci["checks"] != {}  # the previous checks are kept while held
    (event,) = chain.step(none)
    assert (event.payload["from"], event.payload["to"]) == ("passing", "none")
    ci = chain.stored["ci"]
    assert (ci["state"], ci["hold"], ci["hold_to"]) == ("none", 0, None)


def test_none_to_pending_is_held_too() -> None:
    chain = Chain(ci_snap(gh(), "none"))
    pending = settled("pending", check("a", "pending"))
    assert chain.step(pending) == []
    assert chain.stored["ci"]["hold_to"] == "pending" and chain.stored["ci"]["state"] == "none"
    (event,) = chain.step(pending)
    assert (event.payload["from"], event.payload["to"]) == ("none", "pending")


def test_hold_counts_up_to_settle_polls() -> None:
    chain = Chain(settled("passing", check("a")), settle=3)
    none = ci_snap(gh(), "none")
    assert [len(chain.step(none)) for _ in range(3)] == [0, 0, 0]
    assert chain.stored["ci"]["hold"] == 3
    assert [e.type for e in chain.step(none)] == ["ci"]


def test_settle_polls_zero_means_no_hold() -> None:
    chain = Chain(settled("passing", check("a")), settle=0)
    assert chain.types(ci_snap(gh(), "none")) == ["ci"]
    assert chain.stored["ci"]["hold"] == 0


def test_flapping_through_none_resets_the_hold() -> None:
    chain = Chain(settled("passing", check("a")))
    none, ok = ci_snap(gh(), "none"), settled("passing", check("a"))
    assert chain.step(none) == []
    assert chain.stored["ci"]["hold"] == 1
    assert chain.step(ok) == []  # back to the stored state: no event, hold reset
    assert (chain.stored["ci"]["hold"], chain.stored["ci"]["hold_to"]) == (0, None)
    assert chain.step(none) == []
    assert chain.stored["ci"]["hold"] == 1


def test_hold_toward_another_state_restarts_the_count() -> None:
    chain = Chain(ci_snap(gh(), "none"))
    assert chain.step(settled("pending", check("a", "pending"))) == []
    assert chain.stored["ci"]["hold"] == 1
    assert chain.step(settled("failing", check("a", "failing"))) == []
    assert (chain.stored["ci"]["hold"], chain.stored["ci"]["hold_to"]) == (1, "failing")
    assert chain.types(settled("failing", check("a", "failing"))) == ["ci"]


def test_a_non_none_change_cancels_a_pending_hold() -> None:
    chain = Chain(settled("passing", check("a")))
    assert chain.step(ci_snap(gh(), "none")) == []
    assert chain.types(settled("failing", check("a", "failing"))) == ["ci"]
    assert chain.stored["ci"]["hold"] == 0


def test_same_state_updates_checks_without_an_event() -> None:
    chain = Chain(settled("passing", check("a")))
    assert chain.step(settled("passing", check("a"), check("b"))) == []
    assert sorted(v["name"] for v in chain.stored["ci"]["checks"].values()) == ["a", "b"]


def test_ci_incomplete_freezes_state_and_hold() -> None:
    chain = Chain(settled("passing", check("a")))
    assert chain.step(ci_snap(gh(), "none")) == []
    frozen = copy.deepcopy(chain.stored["ci"])
    broken = ci_snap(gh(), "none")
    broken["incomplete"] = ["ci"]
    assert chain.step(broken) == []
    assert chain.stored["ci"] == frozen
    assert [e.type for e in chain.step(ci_snap(gh(), "none"))] == ["ci"]


def test_ci_silent_when_unbaselined_or_rebaseline_and_hold_reset() -> None:
    first = settled("passing", check("a"))
    first["incomplete"] = ["ci"]
    chain = Chain(first)
    assert chain.stored["baselined"]["ci"] is False
    assert chain.step(settled("failing", check("a", "failing"))) == []
    assert chain.stored["ci"]["state"] == "failing"
    assert chain.step(settled("pending", check("a", "pending")), rebaseline=True) == []
    assert chain.stored["ci"]["state"] == "pending"
    assert chain.types(settled("passing", check("a"))) == ["ci"]
    held = Chain(settled("passing", check("a")))
    assert held.step(ci_snap(gh(), "none")) == []
    assert held.stored["ci"]["hold"] == 1
    assert held.step(ci_snap(gh(), "none"), rebaseline=True) == []
    assert (held.stored["ci"]["state"], held.stored["ci"]["hold"]) == ("none", 0)


def test_counts_come_from_the_snapshot_merged_by_key_and_counted_only() -> None:
    chain = Chain(settled("pending", check("a", "pending")))
    snap = settled(
        "failing",
        check("a", "pending"),
        check("a", "failing"),  # same name and url: one check, worst state wins
        check("b", "passing"),
        check("c", "failing", counted=False),
        check("d", "skipped"),
        check("e", "pending"),
    )
    (event,) = chain.step(snap)
    assert event.payload["counts"] == {"passing": 1, "failing": 1, "pending": 1}


def test_counts_are_not_capped_by_the_digest() -> None:
    chain = Chain(settled("pending", check("a", "pending")))
    many = [check(f"c{i:04d}", "passing") for i in range(600)]
    (event,) = chain.step(settled("passing", *many))
    assert event.payload["counts"]["passing"] == 600
    assert len(event.payload["checks"]) == 20
    assert event.payload["checks_more"] > 0


def test_check_changes_appear_disappear_change_ordered_and_capped() -> None:
    old = settled(
        "pending", check("keep", "passing"), check("gone", "pending"), check("flip", "pending")
    )
    chain = Chain(old)
    new = settled(
        "passing", check("keep", "passing"), check("flip", "passing"), check("new", "passing")
    )
    (event,) = chain.step(new)
    assert event.payload["checks"] == [
        {"name": "flip", "from": "pending", "to": "passing"},
        {"name": "gone", "from": "pending", "to": None},
        {"name": "new", "from": None, "to": "passing"},
    ]
    chain = Chain(settled("pending", check("a", "pending")))
    many = [check(f"n{i:02d}", "passing") for i in range(25)]
    (event,) = chain.step(settled("passing", *many))
    assert len(event.payload["checks"]) == 20 and event.payload["checks_more"] == 6


def test_ci_state_change_with_no_check_changes_has_empty_checks() -> None:
    chain = Chain(settled("passing", check("a")))
    (event,) = chain.step(settled("failing", check("a")))
    assert event.payload["checks"] == []


def test_ci_for_head_is_forgectls_head_not_the_prs() -> None:
    chain = Chain(settled("pending", check("a", "pending")))
    (event,) = chain.step(ci_snap(gh(), "passing", check("a"), head=sha(9)))
    assert event.payload["for_head"] == sha(9)
    assert event.payload["head_sha"] == gh()["head"]["sha"]


def test_ci_with_null_for_head_is_dropped_never_invented() -> None:
    # build_digest treats a null ci.head_sha as an incomplete ci, so this takes a hand-made digest
    old = build_digest(settled("pending", check("a", "pending")))
    snap = ci_snap(gh(), "passing", check("a"))
    new = build_digest(snap, old)
    new["ci"]["head_sha"] = None
    result = diff(old, new, snap)
    assert result == []
    assert result.dropped == (Drop("ci", "invalid_head_sha"),)


@pytest.mark.parametrize("backend", [gh, gl])
def test_ci_both_backends(backend: Any) -> None:
    chain = Chain(ci_snap(backend(), "pending", check("a", "pending")))
    assert chain.types(ci_snap(backend(), "passing", check("a"))) == ["ci"]


# --- ready -------------------------------------------------------------------------------------


def test_ready_fires_when_draft_goes_false_with_blocked_by() -> None:
    old = with_(gh(), draft=True, blocked_by=[{"code": "draft"}])
    chain = Chain(old)
    (event,) = chain.step(with_(gh(), draft=False, blocked_by=[{"code": "mergeability_unknown"}]))
    assert event.type == "ready"
    assert event.payload["from_blocked_by"] == [{"code": "draft"}]
    assert event.payload["blocked_by"] == [{"code": "mergeability_unknown"}]
    assert event.payload["occurred_at"] is None


@pytest.mark.parametrize(
    ("before", "after"), [(False, False), (False, True), (True, True), (None, False), (True, None)]
)
def test_ready_only_for_true_to_false(before: Any, after: Any) -> None:
    chain = Chain(with_(gh(), draft=before))
    snap = with_(gh(), draft=after)
    if before is None or after is None:
        snap = with_(gh(), draft=after if after is not None else "x")
    assert "ready" not in chain.types(snap)


def test_ready_with_unreadable_new_draft_keeps_old_and_fires_nothing() -> None:
    chain = Chain(with_(gh(), draft=True))
    assert chain.types(with_(gh(), draft="maybe")) == []
    assert chain.types(with_(gh(), draft=False)) == ["ready"]


def test_ready_silent_under_rebaseline_and_at_first_sight() -> None:
    chain = Chain(with_(gh(), draft=True))
    assert chain.types(with_(gh(), draft=False), rebaseline=True) == []
    first = diff(None, build_digest(gh()), gh())
    assert first == []


def test_ready_fires_once() -> None:
    chain = Chain(with_(gh(), draft=True))
    assert chain.types(with_(gh(), draft=False)) == ["ready"]
    assert chain.types(with_(gh(), draft=False)) == []


# --- ordering, determinism, purity, schemas ----------------------------------------------------


def busy() -> tuple[Chain, dict[str, Any]]:
    old = with_(
        reviewers(gh(), entry("1002", "dismissed", T0)),
        draft=True,
        blocked_by=[{"code": "draft"}],
    )
    old = ci_snap(old, "pending", check("a", "pending"))
    chain = Chain(old)
    new = with_(
        reviewers(
            gh(head={"sha": sha(2)}),
            entry("1002", "approved", T1),
            entry("1003", "changes_requested", T2, login="user-3"),
        ),
        reviews=[review("gh:r:PRR_o", "approved", T1)],
        draft=False,
        blocked_by=[],
    )
    return chain, ci_snap(new, "passing", check("a"))


def test_fixed_order_of_all_event_types() -> None:
    chain, snap = busy()
    assert chain.types(snap) == [
        "pushed",
        "review",
        "approved",
        "changes_requested",
        "ci",
        "ready",
    ]


def test_merged_follows_ready_and_ci() -> None:
    chain, snap = busy()
    snap["state"] = "merged"
    snap["merged_at"] = T3
    assert chain.types(snap)[-3:] == ["ci", "ready", "merged"]


def test_deterministic_and_inputs_untouched() -> None:
    chain, snap = busy()
    old = chain.stored
    new = build_digest(snap, old)
    before = copy.deepcopy((old, new, snap))
    first = diff(old, new, snap)
    second = diff(old, new, snap)
    assert first == second and first.digest == second.digest
    assert (old, new, snap) == before


def test_input_order_of_reviewers_and_checks_does_not_change_output() -> None:
    old = ci_snap(reviewers(gh(), entry("1", "dismissed", T0)), "pending", check("a", "pending"))
    es = [entry(str(100 + i), at=T1) for i in range(4)]
    cs = [check(f"c{i}", "passing") for i in range(4)]
    one = Chain(old).step(ci_snap(reviewers(gh(), *es), "passing", *cs))
    two = Chain(old).step(ci_snap(reviewers(gh(), *reversed(es)), "passing", *reversed(cs)))
    assert one == two


def test_digest_is_a_copy_and_equals_the_input_when_nothing_is_held() -> None:
    old = build_digest(gh())
    snap = with_(gh(), draft=True)
    new = build_digest(snap, old)
    result = diff(old, new, snap)
    assert result.digest == new and result.digest is not new


def test_digest_for_first_sight_and_merged_old_is_the_new_digest() -> None:
    new = build_digest(gh())
    assert diff(None, new, gh()).digest == new
    merged = with_(gh(), state="merged")
    stored = build_digest(merged)
    assert diff(stored, build_digest(gh(), stored), gh()).digest == build_digest(gh(), stored)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with open_state_dir(tmp_path / "pr-watch") as state:
        assert isinstance(state, StateDir)
        with open_store(state) as s:
            yield s


def test_every_new_event_validates_against_the_event_schema(store: Store) -> None:
    chain, snap = busy()
    events = chain.step(snap, forced=lambda a, b: False)
    held = Chain(settled("passing", check("a")))
    held.step(ci_snap(gh(), "none"))
    events += held.step(ci_snap(gh(), "none"))
    gl_chain = Chain(reviewers(gl(), entry("12", "dismissed", None)))
    gl_snap = reviewers(gl(), entry("12", "approved", None))
    gl_snap["latest_by_reviewer"][0].update(commit_sha=None, can_push=None)
    gl_snap["approvals_at_head"] = None
    events += gl_chain.step(gl_snap)
    seen = set()
    with store.transaction():
        for ev in events:
            stored = store.append_event(ev.to_new_event(labels=("k=v",), watches=(WATCH,)))
            assert schema_errors("event", stored.to_json()) == []
            seen.add(ev.type)
    assert {"approved", "changes_requested", "ci", "ready"} <= seen


# --- dropped events ----------------------------------------------------------------------------


def test_invalid_identity_drops_new_events_with_reason_codes() -> None:
    chain, snap = busy()
    snap["pr_id"] = "bad id"
    assert chain.step(snap) == []
    dropped = {(d.type, d.reason) for d in chain.last.dropped}
    assert {("approved", "invalid_identity"), ("ci", "invalid_identity")} <= dropped
    assert ("ready", "invalid_identity") in dropped


def test_no_valid_head_drops_events_except_pushed() -> None:
    old = with_(gh(), draft=True)
    old["head"] = {**old["head"], "sha": "zz"}  # never readable: no head anywhere
    chain = Chain(old)
    snap = with_(gh(), draft=False)
    snap["head"] = {**snap["head"], "sha": "zz"}
    assert chain.step(snap) == []
    assert chain.last.dropped == (Drop("ready", "invalid_head_sha"),)


# --- review round 1 ----------------------------------------------------------------------------


@pytest.mark.parametrize("flip", [False, True])
def test_review_with_unparseable_submitted_at_ranks_lowest_for_the_login(flip: bool) -> None:
    good = review("gh:r:PRR_g", "approved", T1) | {
        "author": {"id": "1002", "login": "good-time", "is_bot": False}
    }
    bad = review("gh:r:PRR_b", "approved", "garbage") | {
        "author": {"id": "1002", "login": "bad-time", "is_bot": False}
    }
    who = _only_reviewer(entry("1002"), reviews=[bad, good] if flip else [good, bad])
    assert who["login"] == "good-time"


@pytest.mark.parametrize(
    "again", ["2026-10-03T12:00:00+02:00", "2026-10-03T10:00:00.000Z", "2026-10-03T10:00:00Z"]
)
def test_same_approval_instant_in_another_form_does_not_fire_again(again: str) -> None:
    chain = Chain(reviewers(gh(), entry(at="2026-10-03T10:00:00Z")))
    assert chain.types(reviewers(gh(), entry(at=again))) == []


def test_different_approval_instant_fires_again() -> None:
    chain = Chain(reviewers(gh(), entry(at="2026-10-03T10:00:00Z")))
    assert chain.types(reviewers(gh(), entry(at="2026-10-03T12:00:00Z"))) == ["approved"]


def test_ready_filters_stored_blocked_by_to_the_schema_shape() -> None:
    chain = Chain(with_(gh(), draft=True))
    chain.stored["blocked_by"] = [
        {"code": "draft"},
        {"code": "draft", "detail": "extra"},
        {"code": "other", "detail": "fine", "junk": 1},
        {"code": "other", "detail": "two\nlines"},
        {"code": "other", "detail": "x" * 257},
        {"code": "other", "detail": 5},
        {"code": "Bad Code"},
        {"detail": "no code"},
        "text",
        None,
    ]
    (event,) = chain.step(with_(gh(), draft=False))
    shown = event.payload["from_blocked_by"]
    assert shown == [{"code": "draft"}, {"code": "draft"}, {"code": "other", "detail": "fine"}]
    assert schema_errors("defs", shown, "blocked_by") == []


@pytest.mark.parametrize("hold", [-5, "3", None, True, 1.5])
def test_stored_hold_counter_is_clamped_when_carried(hold: Any) -> None:
    chain = Chain(settled("passing", check("a")), settle=0)
    chain.stored["ci"]["hold"] = hold
    chain.stored["ci"]["hold_to"] = "none"
    assert chain.types(ci_snap(gh(), "none")) == ["ci"]


def test_diff_results_do_not_share_a_digest() -> None:
    first, second = DiffResult(), DiffResult()
    first.digest["x"] = 1
    assert second.digest == {}
