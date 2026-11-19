"""The diff: (old digest, new digest, new snapshot) to ordered events (docs/interface.md, Events).

Covers #14: pushed, comment, review_comment, review, merged, closed. Each rule is a table of
(rule text, old snapshot change, new snapshot change, expected event or none).
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pr_watch.diff import RULES, DiffEvent, DiffResult, Drop, diff, forced_from_compare
from pr_watch.digest import build_digest
from pr_watch.state_dir import StateDir, open_state_dir
from pr_watch.store import MAX_SAFE_INT, Store, open_store
from tests.forgectl_support import new_pr, sha
from tests.harness import schema_errors

WATCH = "w-aaaaaaaaaa"
T1 = "2026-10-03T10:00:00Z"
T2 = "2026-10-03T11:00:00Z"
T3 = "2026-10-03T12:00:00Z"


def gh(**changes: Any) -> dict[str, Any]:
    return new_pr(**changes)


def gl(**changes: Any) -> dict[str, Any]:
    return new_pr("gitlab.com", "example-org/widgets", backend="gitlab", **changes)


def author(n: int = 9, login: Any = "user-9", is_bot: Any = False) -> dict[str, Any]:
    return {"id": str(n), "login": login, "is_bot": is_bot}


def note(cid: str, created: str = T1, who: Any = None, minimized: bool = False) -> dict[str, Any]:
    return {
        "author": who if who is not None else author(),
        "body": "text",
        "created_at": created,
        "id": cid,
        "minimized": minimized,
        "reply_to": None,
        "review_id": None,
        "updated_at": created,
    }


def thread(tid: str, comments: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "comments": comments,
        "commit_sha": None,
        "id": tid,
        "line": None,
        "outdated": False,
        "path": None,
        "resolvable": False,
        "resolved": False,
        "side": None,
        "start_line": None,
    }
    return {**base, **kw}


def general(cid: str = "gh:ic:IC_new1", created: str = T1, **kw: Any) -> dict[str, Any]:
    return thread(cid, [note(cid, created, **kw)])


def inline(cid: str = "gh:rc:PRRC_new1", created: str = T1, **kw: Any) -> dict[str, Any]:
    return thread(
        "gh:rt:PRRT_new1",
        [note(cid, created)],
        path="src/app.py",
        line=12,
        side="right",
        commit_sha=sha(3),
        resolvable=True,
        **kw,
    )


def review(rid: str = "gh:r:PRR_new1", state: str = "commented", at: str = T1) -> dict[str, Any]:
    return {
        "author": author(5, "user-5"),
        "body": "b",
        "commit_sha": sha(3),
        "id": rid,
        "state": state,
        "submitted_at": at,
    }


def run(
    old_snap: dict[str, Any] | None,
    new_snap: dict[str, Any],
    **kw: Any,
) -> DiffResult:
    old = build_digest(old_snap) if old_snap is not None else None
    rebaseline = bool(kw.get("rebaseline"))
    new = build_digest(new_snap, old, rebaseline=rebaseline)
    return diff(old, new, new_snap, **kw)


def types(events: list[DiffEvent]) -> list[str]:
    return [e.type for e in events]


def with_(snap: dict[str, Any], **changes: Any) -> dict[str, Any]:
    out = copy.deepcopy(snap)
    out.update(copy.deepcopy(changes))
    return out


def plus_threads(snap: dict[str, Any], *extra: dict[str, Any]) -> dict[str, Any]:
    return with_(snap, threads=[*snap["threads"], *extra])


def plus_reviews(snap: dict[str, Any], *extra: dict[str, Any]) -> dict[str, Any]:
    return with_(snap, reviews=[*snap["reviews"], *extra])


# --- pushed ------------------------------------------------------------------------------------


def test_pushed_fires_with_shas_and_occurred_at() -> None:
    old = gh()
    new = gh(head={"sha": sha(2), "pushed_at": "2026-10-03T09:30:00+02:00"})
    (event,) = run(old, new, forced=lambda a, b: True)
    assert event.type == "pushed"
    assert event.payload["from_sha"] == old["head"]["sha"]
    assert event.payload["to_sha"] == sha(2)
    assert event.payload["forced"] is True
    assert event.payload["head_sha"] == sha(2)
    assert event.payload["occurred_at"] == "2026-10-03T07:30:00.000000Z"
    assert event.payload["repo"] == "github.com/example-org/widgets"
    assert event.pr == new["pr"] and event.pr_id == new["pr_id"]


@pytest.mark.parametrize("backend", [gh, gl])
def test_pushed_both_backends(backend: Any) -> None:
    assert types(run(backend(), backend(head={"sha": sha(2)}))) == ["pushed"]


def test_pushed_not_keyed_on_pushed_at() -> None:
    # same head, a different (earlier or later) pushed_at is no event
    assert run(gh(), gh(head={"pushed_at": "2026-10-03T00:00:00Z"})) == []


def test_pushed_same_head_nothing() -> None:
    assert run(gh(), gh()) == []


def test_pushed_unreadable_new_head_keeps_old_and_fires_nothing() -> None:
    # the digest keeps the previous head for an unreadable one
    assert run(gh(), gh(head={"sha": None})) == []


def test_pushed_no_old_head_fires_nothing() -> None:
    old = build_digest(gh(head={"sha": None}))
    assert old["head_sha"] is None
    new_snap = gh(head={"sha": sha(2)})
    new = build_digest(new_snap, old)
    assert diff(old, new, new_snap) == []


def test_pushed_null_new_head_fires_nothing() -> None:
    old = build_digest(gh())
    new = copy.deepcopy(old)
    new["head_sha"] = None
    assert diff(old, new, gh()) == []


def test_pushed_missing_pushed_at_is_null_occurred_at() -> None:
    (event,) = run(gh(), gh(head={"sha": sha(2), "pushed_at": None}))
    assert event.payload["occurred_at"] is None


def test_pushed_forced_default_is_null_and_callable_gets_shas() -> None:
    (event,) = run(gh(), gh(head={"sha": sha(2)}))
    assert event.payload["forced"] is None
    calls: list[tuple[str, str]] = []

    def spy(a: str, b: str) -> bool | None:
        calls.append((a, b))
        return False

    (event,) = run(gh(), gh(head={"sha": sha(2)}), forced=spy)
    assert calls == [(gh()["head"]["sha"], sha(2))]
    assert event.payload["forced"] is False


def test_forced_callable_not_called_without_a_push() -> None:
    def boom(a: str, b: str) -> bool | None:
        raise AssertionError("compare must only run for a push")

    assert run(gh(), gh(), forced=boom) == []
    assert run(None, gh(head={"sha": sha(2)}), forced=boom) == []


def test_forced_non_bool_result_is_null() -> None:
    (event,) = run(gh(), gh(head={"sha": sha(2)}), forced=lambda a, b: "yes")  # type: ignore[arg-type,return-value]
    assert event.payload["forced"] is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("diverged", True),
        ("behind", True),
        ("unavailable", True),
        ("ahead", False),
        ("identical", None),
        ("too_large", None),
        ("surprise", None),
        (None, None),
    ],
)
def test_forced_from_compare_table(status: str | None, expected: bool | None) -> None:
    assert forced_from_compare(status) is expected


def test_pushed_64_hex_head_validates_against_the_schema(store: Store) -> None:
    # schemas/defs.json sha is 40 or 64 lowercase hex (a sha-256 repository)
    old_snap, new_snap = gh(head={"sha": "a" * 64}), gh(head={"sha": "b" * 64})
    (event,) = run(old_snap, new_snap)
    assert event.payload["head_sha"] == "b" * 64
    with store.transaction():
        stored = store.append_event(event.to_new_event(labels=(), watches=(WATCH,)))
    assert schema_errors("event", stored.to_json()) == []


def test_41_hex_sha_is_refused_by_schema_digest_and_diff() -> None:
    assert schema_errors("defs", "b" * 41, "sha") != []
    assert schema_errors("defs", "b" * 40, "sha") == []
    assert schema_errors("defs", "b" * 64, "sha") == []
    snap = gh(head={"sha": "b" * 41})
    assert build_digest(snap)["head_sha"] is None
    result = run(gh(), snap)
    assert result == [] and result.dropped == ()


def test_mixed_length_push_is_one_valid_event(store: Store) -> None:
    (event,) = run(gh(), gh(head={"sha": "b" * 64}))
    with store.transaction():
        stored = store.append_event(event.to_new_event(labels=(), watches=(WATCH,)))
    assert schema_errors("event", stored.to_json()) == []


# --- comment and review_comment ----------------------------------------------------------------


def test_comment_fires_for_new_general_comment() -> None:
    old = gh()
    (event,) = run(old, plus_threads(old, general("gh:ic:IC_new1", T2)))
    assert event.type == "comment"
    assert event.payload["comment_id"] == "gh:ic:IC_new1"
    assert event.payload["thread_id"] is None
    assert event.payload["author"] == {"id": "9", "login": "user-9", "is_bot": False}
    assert event.payload["occurred_at"] == "2026-10-03T11:00:00.000000Z"
    assert "path" not in event.payload


def test_comment_already_seen_is_not_an_event() -> None:
    old = plus_threads(gh(), general("gh:ic:IC_new1"))
    assert run(old, copy.deepcopy(old)) == []


def test_comment_edit_is_not_an_event() -> None:
    old = plus_threads(gh(), general("gh:ic:IC_new1"))
    new = copy.deepcopy(old)
    new["threads"][-1]["comments"][0]["updated_at"] = T3
    assert run(old, new) == []


def test_comment_first_seen_minimized_is_silent_then_seen() -> None:
    old = gh()
    new = plus_threads(old, general("gh:ic:IC_new1", minimized=True))
    assert run(old, new) == []
    assert "gh:ic:IC_new1" in build_digest(new)["seen"]["comments"]


def test_comment_deleted_is_not_an_event() -> None:
    old = plus_threads(gh(), general("gh:ic:IC_new1"))
    assert run(old, gh()) == []


def test_review_comment_fires_with_position_fields() -> None:
    old = gh()
    (event,) = run(old, plus_threads(old, inline("gh:rc:PRRC_new1", T2)))
    assert event.type == "review_comment"
    assert event.payload["comment_id"] == "gh:rc:PRRC_new1"
    assert event.payload["thread_id"] == "gh:rt:PRRT_new1"
    assert event.payload["path"] == "src/app.py"
    assert event.payload["line"] == 12
    assert event.payload["side"] == "right"
    assert event.payload["commit_sha"] == sha(3)


def test_review_comment_reply_in_old_thread_fires() -> None:
    old = plus_threads(gh(), inline("gh:rc:PRRC_new1", T1))
    new = copy.deepcopy(old)
    new["threads"][-1]["comments"].append(note("gh:rc:PRRC_new2", T2))
    (event,) = run(old, new)
    assert event.payload["comment_id"] == "gh:rc:PRRC_new2"
    assert event.payload["thread_id"] == "gh:rt:PRRT_new1"


def test_review_comment_null_path_line_side_sha() -> None:
    odd = inline()
    odd.update(path=None, line=None, side=None, commit_sha=None)
    (event,) = run(gh(), plus_threads(gh(), odd))
    assert event.payload["path"] == ""
    assert event.payload["line"] is None
    assert event.payload["side"] is None
    assert event.payload["commit_sha"] is None


def test_review_comment_bad_line_and_side_become_null() -> None:
    for line, side in ((True, "x y"), (-1, "toolongsidevalue12345"), ("3", 5)):
        odd = inline()
        odd.update(line=line, side=side)
        (event,) = run(gh(), plus_threads(gh(), odd))
        assert event.payload["line"] is None and event.payload["side"] is None


def test_gitlab_unpositioned_note_is_comment_with_discussion_thread() -> None:
    d = thread("gl:d:" + "e" * 40, [note("gl:n:910", T2)])
    (event,) = run(gl(), plus_threads(gl(), d))
    assert event.type == "comment"
    assert event.payload["comment_id"] == "gl:n:910"
    assert event.payload["thread_id"] == "gl:d:" + "e" * 40


def test_gitlab_positioned_note_is_review_comment() -> None:
    d = thread(
        "gl:d:" + "f" * 40,
        [note("gl:n:911", T2)],
        path="src/w.py",
        line=4,
        side="right",
        commit_sha=sha(3),
    )
    (event,) = run(gl(), plus_threads(gl(), d))
    assert event.type == "review_comment"
    assert event.payload["thread_id"] == "gl:d:" + "f" * 40
    assert event.payload["path"] == "src/w.py"


def test_gitlab_reply_in_old_discussion_fires() -> None:
    old = gl()
    new = copy.deepcopy(old)
    new["threads"][1]["comments"].append(note("gl:n:999", T3))
    (event,) = run(old, new)
    assert event.type == "review_comment"  # that discussion is positioned
    assert event.payload["comment_id"] == "gl:n:999"


def test_gitlab_never_emits_review() -> None:
    assert [
        e for e in run(gl(), plus_threads(gl(), general("gl:n:77"))) if e.type == "review"
    ] == []


@pytest.mark.parametrize(
    "bad_id", ["gh:ic:bad id", "gh:xx:IC_1", "plain", "gl:n:0", "gh:ic:" + "a" * 120]
)
def test_comment_with_unrecognized_or_invalid_id_is_no_event(bad_id: str) -> None:
    assert run(gh(), plus_threads(gh(), general(bad_id))) == []


def test_comment_author_variants() -> None:
    cases = [
        (None, {"id": None, "login": None, "is_bot": None}),
        (
            {"id": "bad id", "login": "x", "is_bot": "yes"},
            {"id": None, "login": "x", "is_bot": None},
        ),
        ({"id": "7", "login": None, "is_bot": True}, {"id": "7", "login": None, "is_bot": True}),
    ]
    for given, expected in cases:
        t = general("gh:ic:IC_a1")
        t["comments"][0]["author"] = given
        (event,) = run(gh(), plus_threads(gh(), t))
        assert event.payload["author"] == expected


def test_hostile_one_line_text_stays_bounded() -> None:
    nasty = "evil\n\x1b[31m‮" + "x" * 5000
    t = inline()
    t["path"] = nasty
    t["comments"][0]["author"] = author(login=nasty)
    (event,) = run(gh(), plus_threads(gh(), t))
    for text in (event.payload["path"], event.payload["author"]["login"]):
        assert len(text) <= 256
        assert "\n" not in text and "\x1b" not in text and "‮" not in text


def test_comments_in_one_snapshot_ordered_by_created_then_id() -> None:
    old = gh()
    new = plus_threads(
        old,
        general("gh:ic:IC_c", T2),
        general("gh:ic:IC_b", T2),
        general("gh:ic:IC_a", T3),
        general("gh:ic:IC_z", T1),
    )
    ids = [e.payload["comment_id"] for e in run(old, new)]
    assert ids == ["gh:ic:IC_z", "gh:ic:IC_b", "gh:ic:IC_c", "gh:ic:IC_a"]


def test_comment_ordering_compares_instants_not_text() -> None:
    old = gh()
    new = plus_threads(
        old,
        general("gh:ic:IC_a", "2026-10-03T12:00:00+05:00"),  # 07:00Z
        general("gh:ic:IC_b", "2026-10-03T08:00:00Z"),
    )
    ids = [e.payload["comment_id"] for e in run(old, new)]
    assert ids == ["gh:ic:IC_a", "gh:ic:IC_b"]


def test_comment_unparseable_time_is_null_occurred_at_and_sorts_last() -> None:
    old = gh()
    new = plus_threads(old, general("gh:ic:IC_a", "garbage"), general("gh:ic:IC_b", T1))
    events = run(old, new)
    assert [e.payload["comment_id"] for e in events] == ["gh:ic:IC_b", "gh:ic:IC_a"]
    assert events[1].payload["occurred_at"] is None


def test_duplicate_comment_id_fires_once() -> None:
    old = gh()
    new = plus_threads(old, general("gh:ic:IC_a", T1), general("gh:ic:IC_a", T1))
    assert len(run(old, new)) == 1


def test_threads_incomplete_emits_no_comment_events() -> None:
    old = gh()
    new = plus_threads(old, general())
    new["incomplete"] = ["threads"]
    assert run(old, new) == []


# --- review ------------------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["approved", "changes_requested", "commented", "dismissed"])
def test_review_fires_for_new_review(state: str) -> None:
    old = gh()
    (event,) = run(old, plus_reviews(old, review(state=state, at=T2)))
    assert event.type == "review"
    assert event.payload["review_id"] == "gh:r:PRR_new1"
    assert event.payload["review_state"] == state
    assert event.payload["author"] == {"id": "9", "login": "user-9", "is_bot": False} | {
        "id": "5",
        "login": "user-5",
    }
    assert event.payload["occurred_at"] == "2026-10-03T11:00:00.000000Z"


def test_review_seen_is_not_an_event() -> None:
    old = plus_reviews(gh(), review())
    assert run(old, copy.deepcopy(old)) == []


def test_review_unknown_state_is_no_event() -> None:
    assert run(gh(), plus_reviews(gh(), review(state="pending"))) == []


def test_review_reviews_incomplete_emits_nothing() -> None:
    new = plus_reviews(gh(), review())
    new["incomplete"] = ["reviews"]
    assert run(gh(), new) == []


def test_reviews_ordered_by_submitted_then_id() -> None:
    old = gh()
    new = plus_reviews(
        old,
        review("gh:r:PRR_b", at=T2),
        review("gh:r:PRR_a", at=T2),
        review("gh:r:PRR_c", at=T1),
    )
    assert [e.payload["review_id"] for e in run(old, new)] == [
        "gh:r:PRR_c",
        "gh:r:PRR_a",
        "gh:r:PRR_b",
    ]


# --- merged and closed -------------------------------------------------------------------------


def merged_snap(base: dict[str, Any]) -> dict[str, Any]:
    return with_(
        base,
        state="merged",
        merged_at=T2,
        merged_head_sha=sha(4),
        merge_commit_sha=sha(5),
    )


def test_merged_fires_when_state_becomes_merged() -> None:
    (event,) = run(gh(), merged_snap(gh()))
    assert event.type == "merged"
    assert event.payload["merged_head_sha"] == sha(4)
    assert event.payload["merge_commit_sha"] == sha(5)
    assert event.payload["occurred_at"] == "2026-10-03T11:00:00.000000Z"


def test_merged_null_shas_and_time() -> None:
    (event,) = run(gh(), with_(gh(), state="merged"))
    assert event.payload["merged_head_sha"] is None
    assert event.payload["merge_commit_sha"] is None
    assert event.payload["occurred_at"] is None


def test_closed_fires_when_state_becomes_closed() -> None:
    (event,) = run(gh(), with_(gh(), state="closed", closed_at=T3))
    assert event.type == "closed"
    assert event.payload["occurred_at"] == "2026-10-03T12:00:00.000000Z"


def test_closed_to_merged_fires_merged() -> None:
    assert types(run(with_(gh(), state="closed"), merged_snap(gh()))) == ["merged"]


@pytest.mark.parametrize("terminal", ["merged", "closed"])
def test_unchanged_terminal_state_is_no_event(terminal: str) -> None:
    snap = with_(gh(), state=terminal)
    assert run(snap, copy.deepcopy(snap)) == []


@pytest.mark.parametrize("then", ["open", "closed"])
def test_merged_is_final(then: str) -> None:
    old = merged_snap(gh())
    assert run(old, with_(gh(head={"sha": sha(2)}), state=then)) == []


def test_unknown_old_state_is_first_sight_for_terminal_events() -> None:
    old = build_digest(gh(state="weird"))
    assert old["state"] is None
    new_snap = merged_snap(gh())
    assert types(diff(old, build_digest(new_snap, old), new_snap)) == ["merged"]


def test_unknown_old_state_chain_emits_merged_once() -> None:
    first = build_digest(gh(state="weird"))
    merged = merged_snap(gh())
    second = build_digest(merged, first)
    assert types(diff(first, second, merged)) == ["merged"]
    third = build_digest(merged, second)
    assert diff(second, third, merged) == []


@pytest.mark.parametrize("backend", [gh, gl])
def test_merged_and_closed_on_both_backends(backend: Any) -> None:
    assert types(run(backend(), merged_snap(backend()))) == ["merged"]
    assert types(run(backend(), with_(backend(), state="closed"))) == ["closed"]


# --- first sight and baselines -----------------------------------------------------------------


@pytest.mark.parametrize("backend", [gh, gl])
def test_first_sight_open_emits_nothing(backend: Any) -> None:
    assert run(None, backend()) == []


def test_first_sight_terminal_emits_the_terminal_event_once() -> None:
    assert types(run(None, merged_snap(gh()))) == ["merged"]
    assert types(run(None, with_(gh(), state="closed"))) == ["closed"]


def test_first_sight_terminal_emits_nothing_else() -> None:
    snap = plus_reviews(plus_threads(merged_snap(gh()), general(), inline()), review())
    assert types(run(None, snap)) == ["merged"]


def test_unbaselined_part_is_silent() -> None:
    old = gh()
    old["incomplete"] = ["threads"]
    old_digest = build_digest(old)
    assert old_digest["baselined"]["threads"] is False
    new = plus_threads(gh(), general())
    new_digest = build_digest(new, old_digest)
    assert diff(old_digest, new_digest, new) == []
    assert new_digest["baselined"]["threads"] is True


def test_rebaseline_is_silent_except_pushed_merged_closed() -> None:
    old = gh()
    new = plus_reviews(
        plus_threads(merged_snap(gh(head={"sha": sha(2)})), general(), inline()), review()
    )
    assert types(run(old, new, rebaseline=True)) == ["pushed", "merged"]
    closed = plus_threads(with_(gh(head={"sha": sha(2)}), state="closed"), general())
    assert types(run(old, closed, rebaseline=True)) == ["pushed", "closed"]


def test_malformed_old_digest_counts_as_first_sight() -> None:
    new_snap = plus_threads(gh(), general())
    new = build_digest(new_snap)
    for bad in ("x", 3, [], {}):
        assert diff(bad, new, new_snap) == []  # type: ignore[arg-type]


# --- ordering, determinism, purity -------------------------------------------------------------


def busy_pair() -> tuple[dict[str, Any], dict[str, Any]]:
    old = gh()
    new = plus_reviews(
        plus_threads(
            merged_snap(gh(head={"sha": sha(2)})),
            inline("gh:rc:PRRC_x", T2),
            general("gh:ic:IC_x", T3),
        ),
        review(at=T1),
    )
    return old, new


def test_fixed_type_order_in_one_snapshot() -> None:
    old, new = busy_pair()
    assert types(run(old, new)) == ["pushed", "review", "comment", "review_comment", "merged"]


def test_rule_table_follows_the_documented_order() -> None:
    documented = [
        "pushed",
        "review",
        "comment",
        "review_comment",
        "approved",
        "changes_requested",
        "ci",
        "ready",
        "merged",
        "closed",
    ]
    got = [r.type for r in RULES]
    assert got == [t for t in documented if t in got]
    assert len(set(got)) == len(got)


def test_deterministic_and_inputs_untouched() -> None:
    old, new = busy_pair()
    old_d = build_digest(old)
    new_d = build_digest(new, old_d)
    before = copy.deepcopy((old_d, new_d, new))
    first = diff(old_d, new_d, new)
    second = diff(old_d, new_d, new)
    assert first == second
    assert (old_d, new_d, new) == before


def test_input_order_of_threads_does_not_change_output() -> None:
    old = gh()
    extra = [general(f"gh:ic:IC_{i}", T1) for i in range(5)]
    a = plus_threads(old, *extra)
    b = plus_threads(old, *reversed(extra))
    assert run(old, a) == run(old, b)


# --- schema validity ---------------------------------------------------------------------------


@pytest.fixture
def state(tmp_path: Path) -> Iterator[StateDir]:
    with open_state_dir(tmp_path / "pr-watch") as s:
        yield s


@pytest.fixture
def store(state: StateDir) -> Iterator[Store]:
    with open_store(state) as s:
        yield s


def test_every_event_validates_against_the_event_schema(store: Store) -> None:
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = [busy_pair()]
    pairs.append((gl(), plus_threads(gl(), general("gl:n:5"), inline("gl:n:6"))))
    pairs.append((gh(), with_(gh(), state="closed", closed_at=T1)))
    odd = inline()
    odd.update(path=None, line=None, side=None, commit_sha=None)
    odd["comments"][0]["author"] = None
    pairs.append((gh(), plus_threads(gh(), odd)))
    seen: set[str] = set()
    for old, new in pairs:
        with store.transaction():
            for ev in run(old, new, forced=lambda a, b: True):
                stored = store.append_event(ev.to_new_event(labels=("k=v",), watches=(WATCH,)))
                assert schema_errors("event", stored.to_json()) == []
                seen.add(ev.type)
    assert seen == {"pushed", "review", "comment", "review_comment", "merged", "closed"}


def test_to_new_event_carries_identity_and_watches() -> None:
    (event,) = run(gh(), merged_snap(gh()))
    new_event = event.to_new_event(labels=("a",), watches=(WATCH,))
    assert new_event.type == "merged"
    assert new_event.pr == event.pr and new_event.pr_id == event.pr_id
    assert new_event.labels == ("a",)
    assert new_event.payload["watches"] == [WATCH]
    assert "watches" not in event.payload


# --- dropped events (never silent) ------------------------------------------------------------


def test_result_is_a_list_with_dropped_defaulting_to_empty() -> None:
    result = run(gh(), merged_snap(gh()))
    assert types(result) == ["merged"]
    assert result.dropped == ()


def test_invalid_identity_drops_the_event_with_a_reason_code() -> None:
    new_snap = merged_snap(gh())
    new_snap["pr_id"] = "bad id"
    result = run(gh(), new_snap)
    assert result == []
    assert result.dropped == (Drop("merged", "invalid_identity"),)


def test_review_with_unknown_state_or_bad_id_is_dropped_with_reasons() -> None:
    result = run(gh(), plus_reviews(gh(), review(state="pending")))
    assert result == [] and result.dropped == (Drop("review", "invalid_review_state"),)
    result = run(gh(), plus_reviews(gh(), review(rid="gh:r:" + "a" * 120)))
    assert result == [] and result.dropped == (Drop("review", "invalid_review_id"),)


def test_comment_with_invalid_id_is_dropped_once_under_its_kind() -> None:
    result = run(gh(), plus_threads(gh(), general("gh:ic:" + "a" * 120)))
    assert result.dropped == (Drop("comment", "invalid_comment_id"),)
    odd = inline("gh:rc:" + "a" * 120)
    result = run(gh(), plus_threads(gh(), odd))
    assert result.dropped == (Drop("review_comment", "invalid_comment_id"),)


def test_drop_carries_no_host_text() -> None:
    result = run(gh(), plus_threads(gh(), general("gh:ic:" + "evil" * 30)))
    assert all(set(vars(d)) == {"type", "reason"} for d in result.dropped)
    assert "evil" not in repr(result.dropped)


def test_minimized_comment_is_not_a_drop() -> None:
    new = plus_threads(gh(), general("gh:ic:IC_min", minimized=True))
    assert run(gh(), new).dropped == ()


# --- review_comment line bound (R1) -----------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "kept"), [(MAX_SAFE_INT, True), (MAX_SAFE_INT + 1, False), (0, True)]
)
def test_review_comment_line_bounded_by_store_limit(line: int, kept: bool, store: Store) -> None:
    odd = inline()
    odd["line"] = line
    (event,) = run(gh(), plus_threads(gh(), odd))
    assert event.payload["line"] == (line if kept else None)
    with store.transaction():
        stored = store.append_event(event.to_new_event(labels=(), watches=(WATCH,)))
    assert schema_errors("event", stored.to_json()) == []


# --- invalid new head (R3) ---------------------------------------------------------------------


def null_head_pair() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    old_snap = gh()
    new_snap = plus_threads(old_snap, general())
    old = build_digest(old_snap)
    new = build_digest(new_snap, old)
    new["head_sha"] = None
    return old, new, new_snap


def test_null_new_head_falls_back_to_the_old_head_for_other_events() -> None:
    old, new, new_snap = null_head_pair()
    result = diff(old, new, new_snap)
    assert types(result) == ["comment"]
    assert result[0].payload["head_sha"] == old["head_sha"]
    assert result.dropped == ()


def test_null_new_head_still_suppresses_pushed() -> None:
    old, new, new_snap = null_head_pair()
    assert "pushed" not in types(diff(old, new, new_snap))


def test_no_valid_head_anywhere_drops_non_pushed_events_with_a_reason() -> None:
    old, new, new_snap = null_head_pair()
    old["head_sha"] = None
    result = diff(old, new, new_snap)
    assert result == []
    assert result.dropped == (Drop("comment", "invalid_head_sha"),)


def test_first_sight_terminal_with_null_head_is_dropped_with_a_reason() -> None:
    snap = merged_snap(gh())
    new = build_digest(snap)
    new["head_sha"] = None
    result = diff(None, new, snap)
    assert result == [] and result.dropped == (Drop("merged", "invalid_head_sha"),)


# --- review is GitHub only (R4), reviews gate (R6) ---------------------------------------------


def test_gitlab_never_emits_review_even_with_a_github_style_id() -> None:
    result = run(gl(), plus_reviews(gl(), review("gh:r:PRR_odd")))
    assert [e for e in result if e.type == "review"] == []


def test_review_after_incomplete_reviews_baseline_is_silent() -> None:
    old_snap = gh()
    old_snap["incomplete"] = ["reviews"]
    old = build_digest(old_snap)
    assert old["baselined"]["reviews"] is False
    new_snap = plus_reviews(gh(), review())
    assert diff(old, build_digest(new_snap, old), new_snap) == []
