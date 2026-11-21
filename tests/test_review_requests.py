"""`review_requested` and `converted_to_draft` (1.x, #35): the digest part and the diff rules."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from pr_watch.diff import diff
from pr_watch.digest import MAX_REVIEWERS, build_digest, incomplete_parts
from pr_watch.store import Store
from tests.harness import schema_errors
from tests.test_diff import gh, gl, run, types, with_
from tests.test_diff import state as state  # noqa: F401  (fixtures)
from tests.test_diff import store as store  # noqa: F401

WATCH = "w-aaaaaaaaaa"
T1 = "2026-10-03T10:00:00Z"
T2 = "2026-10-03T11:00:00Z"


def rr(
    login: Any,
    ident: Any = None,
    kind: Any = "user",
    state: str = "outstanding",
    at: Any = T1,
    **kw: Any,
) -> dict[str, Any]:
    return {
        "code_owner": kw.get("code_owner"),
        "instance": kw.get("instance"),
        "requested_at": at,
        "reviewer": {"id": ident, "kind": kind, "login": login},
        "state": state,
    }


def requests(snap: dict[str, Any], *items: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return with_(snap, review_requests=list(items), teams_visible=kw.pop("teams_visible", True))


def events(old: dict[str, Any] | None, new: dict[str, Any], **kw: Any) -> list[Any]:
    return [e for e in run(old, new, **kw) if e.type == "review_requested"]


# --- review_requested --------------------------------------------------------------------------


def test_a_newly_requested_user_fires_with_the_reviewer_and_time() -> None:
    old = requests(gh(), rr("alice", "100"))
    new = requests(gh(), rr("alice", "100"), rr("bob", "200", at=T2, code_owner=True))
    (event,) = events(old, new)
    assert event.payload["reviewer"] == {"id": "200", "login": "bob", "kind": "user"}
    assert event.payload["code_owner"] is True
    assert event.payload["occurred_at"] == "2026-10-03T11:00:00.000000Z"
    assert "requester" not in event.payload


def test_first_sight_is_silent() -> None:
    assert events(None, requests(gh(), rr("alice", "100"))) == []


def test_a_stored_request_does_not_fire_again() -> None:
    snap = requests(gh(), rr("alice", "100"))
    assert events(snap, snap) == []


def test_a_login_only_reviewer_is_keyed_case_insensitively() -> None:
    old = requests(gh(), rr("Alice"))
    assert events(old, requests(gh(), rr("ALICE"))) == []
    (event,) = events(old, requests(gh(), rr("Alice"), rr("Carol")))
    assert event.payload["reviewer"] == {"id": None, "login": "Carol", "kind": "user"}


def test_old_digest_without_the_part_is_silent_and_fills_it() -> None:
    old_snap = requests(gh(), rr("alice", "100"))
    old = build_digest(old_snap)
    del old["review_requests"]
    del old["baselined"]["review_requests"]
    new_snap = requests(gh(), rr("alice", "100"), rr("bob", "200"))
    new = build_digest(new_snap, old)
    assert diff(old, new, new_snap) == []
    assert new["baselined"]["review_requests"] is True
    assert new["review_requests"]["outstanding"] == ["user:100", "user:200"]
    # the next snapshot compares normally
    more = requests(gh(), rr("alice", "100"), rr("bob", "200"), rr("eve", "300"))
    assert [e.type for e in diff(new, build_digest(more, new), more)] == ["review_requested"]


def test_team_and_user_of_the_same_name_are_different_keys() -> None:
    old = requests(gh(), rr("dev", None, "user"))
    new = requests(gh(), rr("dev", None, "user"), rr("dev", None, "team"))
    (event,) = events(old, new)
    assert event.payload["reviewer"] == {"id": None, "login": "dev", "kind": "team"}
    old_t = requests(gh(), rr("dev", "7", "team"))
    assert [
        e.payload["reviewer"]["kind"]
        for e in events(old_t, requests(gh(), rr("dev", "7", "team"), rr("dev", "8")))
    ] == ["user"]


def test_a_review_clears_the_request_and_a_re_request_fires_again_github() -> None:
    first = requests(gh(), rr("alice", "100"))
    cleared = requests(gh())  # a submitted review clears the request
    assert events(first, cleared) == []
    (event,) = events(cleared, first)
    assert event.payload["reviewer"]["login"] == "alice"


def test_gitlab_reviewed_then_outstanding_again_fires() -> None:
    outstanding = requests(gl(), rr("u2", "12", state="outstanding"))
    reviewed = requests(gl(), rr("u2", "12", state="reviewed"))
    assert events(outstanding, reviewed) == []
    assert len(events(reviewed, outstanding)) == 1
    assert events(outstanding, outstanding) == []


def test_gitlab_reviewer_first_seen_as_reviewed_is_no_event_until_outstanding() -> None:
    reviewed = requests(gl(), rr("u2", "12", state="reviewed"))
    assert events(None, reviewed) == []
    assert events(requests(gl()), reviewed) == []


def test_unknown_state_keeps_the_previous_membership() -> None:
    known = requests(gh(), rr("alice", "100"))
    odd = requests(gh(), rr("alice", "100", state="weird"))
    assert events(known, odd) == []
    stored = build_digest(odd, build_digest(known))
    assert stored["review_requests"]["outstanding"] == ["user:100"]  # kept as requested
    assert diff(stored, build_digest(known, stored), known) == []
    assert build_digest(odd, build_digest(requests(gh())))["review_requests"]["outstanding"] == []


def test_rebaseline_is_silent() -> None:
    old = requests(gh())
    new = requests(gh(), rr("alice", "100"))
    assert events(old, new, rebaseline=True) == []


def test_part_listed_incomplete_is_neither_compared_nor_overwritten() -> None:
    old = requests(gh(), rr("alice", "100"))
    snap = requests(gh(), rr("bob", "200"))
    snap["complete"] = False
    snap["incomplete"] = ["review_requests"]
    assert "review_requests" in incomplete_parts(snap)
    assert events(old, snap) == []
    digest = build_digest(snap, build_digest(old))
    assert digest["review_requests"]["outstanding"] == ["user:100"]
    # a later complete snapshot is compared against the kept set, so bob fires once
    done = requests(gh(), rr("bob", "200"))
    assert [e.type for e in diff(digest, build_digest(done, digest), done)] == ["review_requested"]


def test_a_part_never_baselined_while_incomplete_is_filled_silently_later() -> None:
    snap = requests(gh(), rr("alice", "100"))
    snap["incomplete"] = ["review_requests"]
    first = build_digest(snap)
    assert first["baselined"]["review_requests"] is False
    done = requests(gh(), rr("alice", "100"), rr("bob", "200"))
    assert diff(first, build_digest(done, first), done) == []


@pytest.mark.parametrize("value", [None, "x", {"a": 1}, 5])
def test_unreadable_review_requests_make_the_part_incomplete(value: Any) -> None:
    snap = with_(gh(), review_requests=value)
    assert "review_requests" in incomplete_parts(snap)
    assert events(requests(gh(), rr("alice", "100")), snap) == []


def test_too_many_requests_make_the_part_incomplete() -> None:
    items = [rr(f"u{i}", str(i)) for i in range(MAX_REVIEWERS + 1)]
    assert "review_requests" in incomplete_parts(requests(gh(), *items))


def test_missing_review_requests_field_is_incomplete_not_empty() -> None:
    snap = gh()
    del snap["review_requests"]
    assert "review_requests" in incomplete_parts(snap)


def test_teams_not_visible_keeps_stored_teams_and_never_invents_them() -> None:
    old = requests(gh(), rr("alice", "1"), rr("core", "7", "team"))
    blind = requests(gh(), rr("alice", "1"), teams_visible=False)
    assert events(old, blind) == []
    digest = build_digest(blind, build_digest(old))
    assert "team:7" in digest["review_requests"]["outstanding"]
    assert digest["review_requests"]["teams_known"] is False
    # visible again with the same team: no event
    seen = requests(gh(), rr("alice", "1"), rr("core", "7", "team"))
    assert diff(digest, build_digest(seen, digest), seen) == []


def test_a_team_first_visible_after_blindness_is_silent() -> None:
    blind = requests(gh(), rr("alice", "1"), teams_visible=False)
    seen = requests(gh(), rr("alice", "1"), rr("core", "7", "team"))
    assert events(blind, seen) == []
    absent = with_(gh(), review_requests=[rr("alice", "1")])
    del absent["teams_visible"]
    assert events(absent, seen) == []


def test_teams_visible_and_a_team_dropped_is_removed_then_re_requested_fires() -> None:
    with_team = requests(gh(), rr("core", "7", "team"))
    without = requests(gh())
    assert events(with_team, without) == []
    assert len(events(without, with_team)) == 1


def test_events_are_ordered_by_key_and_one_per_reviewer() -> None:
    old = requests(gh())
    new = requests(gh(), rr("zed", "9"), rr("amy", "2"), rr("amy", "2", at=T2))
    out = events(old, new)
    assert [e.payload["reviewer"]["id"] for e in out] == ["2", "9"]


def test_hostile_reviewers_are_skipped_or_sanitized() -> None:
    bad = [
        "x",
        None,
        {"reviewer": None, "state": "outstanding"},
        {"reviewer": {"login": "", "id": None, "kind": "user"}, "state": "outstanding"},
        rr("ghost", "1", kind="robot"),
        rr("ghost", "1", kind=None),
    ]
    old = requests(gh())
    new = with_(gh(), review_requests=bad, teams_visible=True)
    assert events(old, new) == []
    long = "a" * 5000
    (event,) = events(old, requests(gh(), rr(long)))
    assert event.payload["reviewer"] == {"id": None, "login": None, "kind": "user"}  # hashed key
    (event,) = events(old, requests(gh(), rr("a\nb\x07c", "5")))
    assert "\n" not in event.payload["reviewer"]["login"]


def test_an_unreadable_time_is_null_and_a_bad_id_gives_a_null_id() -> None:
    (event,) = events(requests(gh()), requests(gh(), rr("alice", "bad id!", at="yesterday")))
    assert event.payload["occurred_at"] is None
    assert event.payload["reviewer"]["id"] is None
    assert event.payload["code_owner"] is None


def test_digest_part_shape_and_baseline_flag() -> None:
    digest = build_digest(requests(gh(), rr("alice", "100"), rr("dev", None, "team")))
    assert digest["review_requests"] == {
        "outstanding": ["team:login:dev", "user:100"],
        "teams_known": True,
    }
    assert digest["baselined"] == {
        "reviews": True, "threads": True, "ci": True, "review_requests": True
    }  # fmt: skip


def test_corrupt_previous_part_is_replaced_by_the_empty_shape() -> None:
    snap = requests(gh(), rr("a", "1"))
    snap["incomplete"] = ["review_requests"]
    for bad in (None, 5, {"outstanding": "x", "teams_known": True}, {"outstanding": [5]}):
        prev = build_digest(gh())
        prev["review_requests"] = bad
        assert build_digest(snap, prev)["review_requests"] == {
            "outstanding": [], "teams_known": False
        }  # fmt: skip


def test_the_event_validates_against_the_schema(store: Store) -> None:
    old = requests(gh(), rr("alice", "100"))
    new = requests(gh(), rr("alice", "100"), rr("core", "7", "team", code_owner=False))
    with store.transaction():
        for ev in run(old, new):
            stored = store.append_event(ev.to_new_event(labels=("k=v",), watches=(WATCH,)))
            assert schema_errors("event", stored.to_json()) == []
            assert ev.type == "review_requested"


# --- converted_to_draft ------------------------------------------------------------------------


def test_converted_to_draft_fires_when_draft_goes_false_to_true() -> None:
    old = with_(gh(), draft=False, blocked_by=[])
    new = with_(gh(), draft=True, blocked_by=[{"code": "draft"}])
    (event,) = run(old, new)
    assert event.type == "converted_to_draft"
    assert event.payload["from_blocked_by"] == []
    assert event.payload["blocked_by"] == [{"code": "draft"}]
    assert event.payload["occurred_at"] is None


@pytest.mark.parametrize(
    ("was", "now"), [(True, True), (False, False), (None, True), (True, None), ("x", True)]
)
def test_converted_to_draft_needs_two_known_values(was: Any, now: Any) -> None:
    assert "converted_to_draft" not in types(run(with_(gh(), draft=was), with_(gh(), draft=now)))


def test_converted_to_draft_is_silent_on_first_sight_and_rebaseline() -> None:
    assert run(None, with_(gh(), draft=True)) == []
    old, new = with_(gh(), draft=False), with_(gh(), draft=True)
    assert run(old, new, rebaseline=True) == []


def test_ready_and_draft_flapping_fires_each_change_once() -> None:
    steps = [True, False, True, True, False]
    out = []
    for before, after in zip(steps, steps[1:], strict=False):
        out.append(types(run(with_(gh(), draft=before), with_(gh(), draft=after))))
    assert out == [["ready"], ["converted_to_draft"], [], ["ready"]]


def test_converted_to_draft_comes_after_ready_slot_and_before_terminal() -> None:
    old = with_(gh(), draft=False)
    new = with_(gh(), draft=True, state="closed", closed_at=T2)
    assert types(run(old, new)) == ["converted_to_draft", "closed"]


def test_converted_to_draft_event_validates(store: Store) -> None:
    (ev,) = run(with_(gh(), draft=False), with_(gh(), draft=True))
    with store.transaction():
        stored = store.append_event(ev.to_new_event(labels=(), watches=(WATCH,)))
    assert schema_errors("event", stored.to_json()) == []


def test_order_of_a_busy_snapshot() -> None:
    old = with_(requests(gh(), rr("a", "1")), draft=False)
    new = with_(requests(gh(), rr("a", "1"), rr("b", "2")), draft=True)
    assert types(run(old, new)) == ["review_requested", "converted_to_draft"]
    assert copy.deepcopy(new) == new
