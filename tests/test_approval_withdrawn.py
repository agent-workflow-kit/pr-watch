"""`approval_withdrawn` (1.x, #36): a stored `approved` reviewer that is no longer approving."""

from __future__ import annotations

from typing import Any

import pytest

from pr_watch.store import Store
from tests.forgectl_support import sha
from tests.harness import schema_errors
from tests.test_diff import T1, T2, T3, WATCH, gh, gl, review, with_
from tests.test_diff import state as state  # noqa: F401  (fixtures)
from tests.test_diff import store as store  # noqa: F401
from tests.test_diff_approvals import T0, Chain, entry, reviewers

WITHDRAWN = "approval_withdrawn"
NOSHA = {"commit_sha": None}


def approved_gh(*extra: dict[str, Any]) -> dict[str, Any]:
    return reviewers(gh(), entry("1002", "approved", T1, sha(3)), *extra)


def only(chain: Chain, snap: dict[str, Any], **kw: Any) -> list[Any]:
    return [e for e in chain.step(snap, **kw) if e.type == WITHDRAWN]


def test_a_dismissed_approval_fires_with_the_payload() -> None:
    chain = Chain(approved_gh())
    snap = with_(reviewers(gh(), entry("1002", "dismissed", T2, sha(3))), approvals_at_head=0)
    (event,) = only(chain, snap)
    assert event.payload == {
        "repo": snap["repo"],
        "head_sha": snap["head"]["sha"],
        "occurred_at": None,
        "reviewer": {"id": "1002", "login": "user-2", "is_bot": False},
        "to": "dismissed",
        "commit_sha": sha(3),
        "at": "2026-10-03T10:00:00.000000Z",
        "approvals_at_head": 0,
    }


def test_reviewer_account_comes_from_the_reviews_when_present() -> None:
    other = review("gh:r:PRR_a", "approved", T1) | {
        "author": {"id": "1002", "login": "u2", "is_bot": True},
        "submitted_at": T3,
    }
    chain = Chain(reviewers(gh(), entry("1002", "approved", T1)))
    snap = reviewers(gh(), entry("1002", "dismissed", T2))
    snap["reviews"] = [*snap["reviews"], other]
    (event,) = only(chain, snap)
    assert event.payload["reviewer"] == {"id": "1002", "login": "u2", "is_bot": True}


def test_approved_to_changes_requested_fires_only_changes_requested() -> None:
    chain = Chain(approved_gh())
    assert chain.types(reviewers(gh(), entry("1002", "changes_requested", T2))) == [
        "changes_requested"
    ]


def test_a_comment_only_review_after_an_approval_is_no_event() -> None:
    chain = Chain(approved_gh())
    commented = review("gh:r:PRR_c", "commented", T2) | {
        "author": {"id": "1002", "login": "u2", "is_bot": False}
    }
    snap = approved_gh()
    snap["reviews"] = [*snap["reviews"], commented]
    # forgectl's latest_by_reviewer keeps the latest non-commented state, so the entry is unchanged
    assert chain.types(snap) == ["review"]


def test_a_vanished_entry_fires_with_a_null_to() -> None:
    chain = Chain(approved_gh())
    (event,) = only(chain, reviewers(gh()))
    assert event.payload["to"] is None
    assert event.payload["reviewer"] == {"id": "1002", "login": "user-2", "is_bot": False}
    assert event.payload["commit_sha"] == sha(3)


def test_gitlab_unapprove_as_dismissed_has_a_null_approvals_at_head() -> None:
    chain = Chain(reviewers(gl(), entry("12", "approved", T1) | NOSHA, entry("13", "approved", T1)))
    snap = reviewers(gl(), entry("12", "dismissed", None) | NOSHA, entry("13", "approved", T1))
    (event,) = only(chain, snap)
    assert event.payload["to"] == "dismissed" and event.payload["commit_sha"] is None
    assert event.payload["at"] == "2026-10-03T10:00:00.000000Z"
    assert event.payload["approvals_at_head"] is None
    assert event.payload["reviewer"]["id"] == "12"


def test_gitlab_unapprove_as_a_vanished_entry() -> None:
    chain = Chain(reviewers(gl(), entry("12", "approved", T1) | NOSHA))
    (event,) = only(chain, reviewers(gl()))
    assert event.payload["to"] is None and event.payload["approvals_at_head"] is None


@pytest.mark.parametrize("was", ["dismissed", "changes_requested"])
def test_only_a_stored_approval_can_be_withdrawn(was: str) -> None:
    chain = Chain(reviewers(gh(), entry("1002", was, T1)))
    assert chain.types(reviewers(gh())) == []
    chain = Chain(reviewers(gh(), entry("1002", was, T1)))
    assert chain.types(reviewers(gh(), entry("1002", "dismissed", T2))) == []


def test_an_unknown_state_is_never_a_withdrawal() -> None:
    chain = Chain(approved_gh())
    # an unreadable state keeps the stored approval, so a flapping value is silent
    assert chain.types(reviewers(gh(), entry("1002", "weird", T2))) == []
    assert chain.stored["reviewers"]["1002"]["state"] == "approved"
    assert chain.types(reviewers(gh(), entry("1002", "weird2", T3))) == []
    assert chain.types(reviewers(gh(), entry("1002", "dismissed", T3))) == [WITHDRAWN]
    # a stored unknown is no approval
    late = Chain(reviewers(gh(), entry("1002", "weird", T0)))
    assert late.types(reviewers(gh())) == []


def test_an_incomplete_part_is_silent_and_the_withdrawal_comes_with_the_complete_one() -> None:
    chain = Chain(approved_gh())
    snap = reviewers(gh())
    snap["incomplete"] = ["reviews"]
    assert chain.types(snap) == []
    assert chain.stored["reviewers"]["1002"]["state"] == "approved"
    assert chain.types(reviewers(gh())) == [WITHDRAWN]


def test_first_sight_unbaselined_and_rebaseline_are_silent() -> None:
    chain = Chain(approved_gh())
    assert chain.types(reviewers(gh()), rebaseline=True) == []
    assert chain.types(reviewers(gh(), entry("1002", "approved", T2))) == ["approved"]
    unbaselined = approved_gh()
    unbaselined["incomplete"] = ["reviews"]
    late = Chain(unbaselined)
    assert late.stored["baselined"]["reviews"] is False
    assert late.types(reviewers(gh())) == []


def test_first_sight_of_a_dismissed_review_is_silent() -> None:
    from pr_watch.diff import diff
    from pr_watch.digest import build_digest

    snap = reviewers(gh(), entry("1002", "dismissed", T1))
    assert list(diff(None, build_digest(snap), snap)) == []


def test_re_approval_after_a_withdrawal_fires_approved_again() -> None:
    chain = Chain(approved_gh())
    assert chain.types(reviewers(gh(), entry("1002", "dismissed", T2))) == [WITHDRAWN]
    assert chain.types(reviewers(gh(), entry("1002", "approved", T3))) == ["approved"]
    assert chain.types(reviewers(gh())) == [WITHDRAWN]
    assert chain.types(reviewers(gh())) == []  # once


def test_the_same_dismissal_is_one_event() -> None:
    chain = Chain(approved_gh())
    snap = reviewers(gh(), entry("1002", "dismissed", T2))
    assert chain.types(snap) == [WITHDRAWN]
    assert chain.types(snap) == []


def test_two_withdrawals_are_ordered_by_reviewer_key() -> None:
    chain = Chain(reviewers(gh(), entry("2000", at=T1), entry("1500", at=T1), entry("1002", at=T1)))
    events = chain.step(reviewers(gh()))
    assert [e.payload["reviewer"]["id"] for e in events] == ["1002", "1500", "2000"]
    assert [e.type for e in events] == [WITHDRAWN] * 3


def test_a_login_key_flap_reads_as_a_withdrawal_and_a_new_approval() -> None:
    # accepted, as for `approved` (interface, Digest): the digest keeps no login to join the keys
    chain = Chain(reviewers(gh(), entry("1002", "approved", T1)))
    flapped = entry("1002", "approved", T1) | {"id": None, "login": "User-2"}
    assert chain.types(reviewers(gh(), flapped)) == ["approved", WITHDRAWN]


def test_order_changes_requested_then_withdrawn_then_ci() -> None:
    chain = Chain(reviewers(gh(), entry("1", "approved", T1), entry("2", "approved", T1)))
    snap = reviewers(gh(), entry("1", "dismissed", T2), entry("2", "changes_requested", T2))
    snap["ci"] = {**snap["ci"], "state": "failing"}
    assert chain.types(snap) == ["changes_requested", WITHDRAWN, "ci"]


def test_a_new_approval_orders_before_a_withdrawal() -> None:
    chain = Chain(reviewers(gh(), entry("1", "approved", T1)))
    snap = reviewers(gh(), entry("1", "dismissed", T2), entry("2", "approved", T2))
    assert chain.types(snap) == ["approved", WITHDRAWN]


def test_with_no_valid_head_the_event_is_dropped_with_a_reason() -> None:
    chain = Chain(approved_gh())
    snap = reviewers(gh(), entry("1002", "dismissed", T2))
    snap["head"] = {**snap["head"], "sha": "nope"}
    chain.stored["head_sha"] = None
    assert chain.types(snap) == []
    assert [(d.type, d.reason) for d in chain.last.dropped] == [(WITHDRAWN, "invalid_head_sha")]


@pytest.mark.parametrize("backend", ["gh", "gl"])
def test_the_event_validates_against_the_schema(store: Store, backend: str) -> None:
    base = gh if backend == "gh" else gl
    chain = Chain(reviewers(base(), entry("77", "approved", T1)))
    for snap in (reviewers(base(), entry("77", "dismissed", T2)),):
        (event,) = only(chain, snap)
        with store.transaction():
            stored = store.append_event(event.to_new_event(labels=("k=v",), watches=(WATCH,)))
        assert schema_errors("event", stored.to_json()) == []
    chain = Chain(reviewers(base(), entry("77", "approved", T1)))
    (event,) = only(chain, reviewers(base()))
    with store.transaction():
        stored = store.append_event(event.to_new_event(labels=(), watches=(WATCH,)))
    assert schema_errors("event", stored.to_json()) == []


def test_a_push_that_only_lowers_approvals_at_head_is_silent() -> None:
    chain = Chain(with_(approved_gh(), approvals_at_head=1))
    snap = with_(approved_gh(), approvals_at_head=0, head={**gh()["head"], "sha": sha(9)})
    types = chain.types(snap)
    assert "pushed" in types
    assert WITHDRAWN not in types


def test_a_dismissal_with_a_push_fires_withdrawn_together_with_pushed() -> None:
    chain = Chain(with_(approved_gh(), approvals_at_head=1))
    snap = with_(
        reviewers(gh(), entry("1002", "dismissed", T2, sha(3))),
        approvals_at_head=0,
        head={**gh()["head"], "sha": sha(9)},
    )
    types = chain.types(snap)
    assert "pushed" in types
    assert WITHDRAWN in types
